from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
from harness_lib import load_distribution_manifest  # noqa: E402


DISTRIBUTION = load_distribution_manifest(PLUGIN_ROOT)
APPROVED_SKILLS = set(DISTRIBUTION["skills"])
APPROVED_HOOKS = set(DISTRIBUTION["hooks"]["events"])
APPROVED_TOP_LEVEL_COMMANDS = {
    "acceptance",
    "baseline",
    "cycle",
    "decision",
    "delivery",
    "doctor",
    "failure-mode",
    "finding",
    "gate",
    "init",
    "migrate",
    "projection",
    "quickstart",
    "repair",
    "requirement",
    "status",
    "task",
    "test-target",
    "trace",
    "validate",
    "validation",
    "verify",
}
APPROVED_AGENT_TEMPLATES = set(DISTRIBUTION["templates"]["native_agents"])
RETIRED_EXTERNAL_COMMANDS = {
    "connector",
    "connector.profile",
    "connector.profile.set",
    "connector.profile.status",
    "connector.profile.unset",
    "adapter",
    "adapter.ci-verify",
    "adapter.complete",
    "adapter.confirm",
    "adapter.draft",
    "adapter.external-session-verify",
    "adapter.plan",
    "adapter.reconcile",
    "adapter.record",
    "session.attest",
}
RETIRED_RECOVERY_COMMANDS = {
    "checkpoint",
    "checkpoint.create",
    "checkpoint.export",
    "checkpoint.import",
    "checkpoint.list",
    "event",
    "event.export",
    "event.validate",
}
RUNTIME_SCRIPT_NAMES = {
    "harness.py",
    "harness_db.py",
    "harness_lib.py",
}
FORBIDDEN_RUNTIME_LITERALS = {
    "gh api",
    "api.github.com",
    "api.linear.app",
    "api.notion.com",
    "api.figma.com",
    "slack.com/api",
    "github_token",
    "gh_token",
    "linear_api_key",
    "notion_token",
    "figma_token",
    "slack_bot_token",
    "harness_connector_key",
    "connector-key-path",
    "harness_github_api_url",
    "harness_linear_api_url",
    "harness_notion_api_url",
    "harness_figma_api_url",
    "harness_slack_api_url",
}
FORBIDDEN_PROVIDER_IMPORTS = {
    "github",
    "linear",
    "notion_client",
    "figma",
    "slack_sdk",
}


def cli_surface(parser: argparse.ArgumentParser) -> set[str]:
    surface: set[str] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    path = prefix + (name,)
                    surface.add(".".join(path))
                    walk(subparser, path)

    walk(parser)
    return surface


def run_harness(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = os.environ.copy()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "LINEAR_API_KEY", "NOTION_TOKEN", "FIGMA_TOKEN", "SLACK_BOT_TOKEN"):
        clean_env.pop(key, None)
    if env:
        clean_env.update(env)
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=clean_env,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalCoreSurfaceBudgetTests(unittest.TestCase):
    def test_cli_parser_budget_is_exactly_61(self) -> None:
        surface = cli_surface(harness.build_parser())
        self.assertEqual(len(surface), 61, sorted(surface))

    def test_cli_top_level_matches_the_locked_local_domains(self) -> None:
        parser = harness.build_parser()
        choices: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                choices = set(action.choices)
                break
        self.assertEqual(choices, APPROVED_TOP_LEVEL_COMMANDS)

    def test_request_id_command_log_surface_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            rejected = run_harness(
                root,
                "decision",
                "record",
                "--decision",
                "duplicate layer",
                "--reason",
                "removed in v2",
                "--request-id",
                "retired",
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                    )
                }
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unrecognized arguments", (rejected.stdout + rejected.stderr).lower())
        self.assertNotIn("command_log", tables)

    def test_skill_inventory_is_exactly_the_seven_local_entrypoints(self) -> None:
        actual = {path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir()}
        self.assertEqual(len(APPROVED_SKILLS), 7)
        self.assertEqual(actual, APPROVED_SKILLS)

    def test_hook_inventory_is_exactly_three(self) -> None:
        payload = json.loads((PLUGIN_ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
        actual = set(payload["hooks"])
        self.assertEqual(len(APPROVED_HOOKS), 3)
        self.assertEqual(actual, APPROVED_HOOKS)

    def test_agent_template_inventory_is_exactly_three_and_local_only(self) -> None:
        template_root = PLUGIN_ROOT / "templates/agents"
        actual = {path.name for path in template_root.iterdir() if path.is_file()}
        self.assertEqual(len(APPROVED_AGENT_TEMPLATES), 3)
        self.assertEqual(actual, APPROVED_AGENT_TEMPLATES)
        combined = ""
        for name in sorted(APPROVED_AGENT_TEMPLATES):
            payload = tomllib.loads((template_root / name).read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"name", "description", "developer_instructions"})
            self.assertEqual(payload["name"], name.removesuffix(".toml"))
            combined += "\n" + payload["developer_instructions"].lower()
        for retired_term in (
            "github",
            "linear",
            "notion",
            "figma",
            "slack",
            "connector",
            "host sdk",
            "provider",
            "receipt",
        ):
            self.assertNotIn(retired_term, combined)
        self.assertIn("native codex/chatgpt owns", combined)
        self.assertIn("root controller", combined)

    def test_project_harness_is_the_consolidated_delivery_entrypoint(self) -> None:
        skill = (PLUGIN_ROOT / "skills/project-harness/SKILL.md").read_text(encoding="utf-8")
        for marker in (
            "OpenSpec is the specification authority",
            "Bootstrap The Workspace",
            "Specification And Requirement Baseline",
            "Team And Delegation",
            "Local Runtime Commands",
            "Root-Owned Task Lifecycle",
            "Immutable Verification",
            "Quality Review And Delivery Handoff",
            "human-review-required",
        ):
            self.assertIn(marker, skill)
        for retired_entrypoint in (
            "`project-bootstrap`",
            "`project-runtime`",
            "`requirement-baseline`",
            "`team-architecture`",
        ):
            self.assertNotIn(retired_entrypoint, skill)
        self.assertFalse((PLUGIN_ROOT / "skills/delivery-readiness").exists())
        self.assertTrue((PLUGIN_ROOT / "skills/project-harness/scripts/harness.py").is_file())


class LocalOnlyRuntimeContractTests(unittest.TestCase):
    def test_plugin_kernel_has_no_external_provider_runtime(self) -> None:
        runtime_sources = sorted((PLUGIN_ROOT / "core").glob("*.py"))
        runtime_sources.extend(
            sorted(path for path in SCRIPTS.glob("*.py") if path.name in RUNTIME_SCRIPT_NAMES)
        )
        runtime_sources.extend(sorted((PLUGIN_ROOT / "hooks").glob("*.py")))
        literal_hits: list[str] = []
        import_hits: list[str] = []
        for path in runtime_sources:
            source = path.read_text(encoding="utf-8")
            lowered = source.lower()
            for literal in FORBIDDEN_RUNTIME_LITERALS:
                if literal in lowered:
                    literal_hits.append(f"{path.relative_to(PLUGIN_ROOT)}:{literal}")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module]
                for module in imported:
                    root_module = module.split(".", 1)[0]
                    if root_module in FORBIDDEN_PROVIDER_IMPORTS:
                        import_hits.append(f"{path.relative_to(PLUGIN_ROOT)}:{module}")

        self.assertEqual(literal_hits, [])
        self.assertEqual(import_hits, [])

    def test_greenfield_requires_no_external_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            status = run_harness(root, "status")

        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

    def test_retired_connector_and_adapter_commands_fail_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            db = root / ".ai-team/state/harness.db"
            before = digest(db)
            results = [
                run_harness(root, "connector", "profile", "status"),
                run_harness(
                    root,
                    "adapter",
                    "record",
                    "--tool",
                    "github",
                    "--mode",
                    "read-only",
                    "--artifact",
                    "retired",
                    "--idempotency-key",
                    "retired-command-test",
                ),
            ]
            after = digest(db)

        for result in results:
            output = (result.stdout + result.stderr).lower()
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("removed", output)
            self.assertIn("v2", output)
        self.assertEqual(after, before)

    def test_greenfield_omits_external_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_harness(root, "init")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            external_views = {
                root / ".ai-team/control/tooling-map.md",
                root / ".ai-team/control/advisory-fallbacks.md",
            }
            existing = {path.relative_to(root).as_posix() for path in external_views if path.exists()}

        self.assertEqual(existing, set())

    def test_local_projection_headers_have_no_external_only_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_harness(root, "init")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            generated = [
                *root.glob(".ai-team/**/*.md"),
                *root.glob("docs/harness/*.md"),
            ]
            text = "\n".join(path.read_text(encoding="utf-8") for path in generated)

        for retired_heading in (
            "Tool Link",
            "External ID",
            "Connector Project Key",
            "GitHub",
            "Linear",
            "Notion",
            "Figma",
            "Slack",
        ):
            self.assertNotIn(retired_heading, text)
        self.assertFalse(hasattr(__import__("harness_db"), "render_tooling_map"))
        self.assertFalse(hasattr(__import__("harness_db"), "render_evidence"))

    def test_task_board_template_header_matches_renderer_contract(self) -> None:
        expected = [
            "| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Evidence | Producer Context | Revision |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        template = (PLUGIN_ROOT / "templates/project/task-board.md").read_text(encoding="utf-8").splitlines()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_harness(root, "init")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = (root / ".ai-team/planning/task-board.md").read_text(encoding="utf-8").splitlines()

        self.assertEqual(template[2:4], expected)
        self.assertEqual(rendered[2:4], expected)
        self.assertEqual(template[2:4], rendered[2:4])
        self.assertNotIn("Tool Link", "\n".join(template[2:4]))

    def test_retired_external_commands_are_absent_from_public_cli(self) -> None:
        actual = cli_surface(harness.build_parser())
        self.assertEqual(actual & RETIRED_EXTERNAL_COMMANDS, set())

    def test_retired_event_recovery_commands_are_absent_from_public_cli(self) -> None:
        actual = cli_surface(harness.build_parser())
        self.assertEqual(actual & RETIRED_RECOVERY_COMMANDS, set())

    def test_same_process_hmac_trust_surface_is_removed(self) -> None:
        actual = cli_surface(harness.build_parser())
        self.assertFalse((PLUGIN_ROOT / "core/connector_trust.py").exists())
        self.assertNotIn("session.attest", actual)
        self.assertNotIn("adapter.ci-verify", actual)
        self.assertNotIn("adapter.external-session-verify", actual)


if __name__ == "__main__":
    unittest.main()

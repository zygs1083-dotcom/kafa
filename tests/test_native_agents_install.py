from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
SCRIPTS = REPO_ROOT / "plugins/codex-project-harness/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness_db  # noqa: E402


APPROVED_TEMPLATES = {"architect.toml", "developer.toml", "qa-reviewer.toml"}
TEMPLATE_ROOT = REPO_ROOT / "plugins/codex-project-harness/templates"


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


class NativeAgentsInstallTest(unittest.TestCase):
    def test_project_init_installs_exactly_three_native_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_harness(root, "init")
            actual = {path.name for path in (root / ".codex/agents").glob("*.toml")}

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(actual, APPROVED_TEMPLATES)

    def test_project_init_preserves_existing_native_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_dir = root / ".codex/agents"
            agent_dir.mkdir(parents=True)
            developer = agent_dir / "developer.toml"
            developer.write_text("custom = true\n", encoding="utf-8")

            result = run_harness(root, "init")
            actual = {path.name for path in agent_dir.glob("*.toml")}
            preserved = developer.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(actual, APPROVED_TEMPLATES)
        self.assertEqual(preserved, "custom = true\n")

    def test_retired_agents_command_does_not_mutate_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            before = {
                path.name: path.read_bytes() for path in (root / ".codex/agents").glob("*.toml")
            }
            retired = run_harness(root, "agents", "install")
            after = {
                path.name: path.read_bytes() for path in (root / ".codex/agents").glob("*.toml")
            }

        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertNotEqual(retired.returncode, 0)
        self.assertIn("removed in Kafa v2", retired.stdout)
        self.assertEqual(after, before)

    def test_agent_template_schema_rejects_missing_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.toml"
            path.write_text('name = "bad"\ndescription = "Bad"\ncustom = true\n', encoding="utf-8")

            with self.assertRaises(harness_db.HarnessError) as raised:
                harness_db.validate_codex_agent_template(path)

        self.assertIn("missing developer_instructions", str(raised.exception))

    def test_agent_templates_enforce_root_only_facts_and_read_only_review(self) -> None:
        templates = {
            path.stem: path.read_text(encoding="utf-8").lower()
            for path in (TEMPLATE_ROOT / "agents").glob("*.toml")
        }

        self.assertEqual(set(templates), {"architect", "developer", "qa-reviewer"})
        for name, text in templates.items():
            self.assertIn("do not write kafa facts", text, name)
            self.assertIn("root controller", text, name)
            self.assertIn("return", text, name)
        self.assertNotIn("notes worth recording in local project or kafa artifacts", templates["architect"])
        self.assertIn("do not edit the candidate", templates["qa-reviewer"])
        self.assertIn("re-review", templates["qa-reviewer"])

    def test_project_agents_puts_root_single_writer_before_state_guidance(self) -> None:
        text = (TEMPLATE_ROOT / "project/AGENTS.md").read_text(encoding="utf-8").lower()

        self.assertNotIn("update phase", text)
        root_only = text.index("only the root controller")
        state_guidance = text.index(".ai-team/")
        self.assertLess(root_only, state_guidance)
        self.assertIn("subagents return", text)


if __name__ == "__main__":
    unittest.main()

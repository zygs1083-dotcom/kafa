from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DocumentationContractTest(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def test_current_guidance_is_not_a_version_chronology(self) -> None:
        for relative in [
            "README.md",
            "docs/runtime/CONTROL_PLANE.md",
            "plugins/codex-project-harness/skills/project-runtime/SKILL.md",
        ]:
            text = self.read(relative)
            self.assertIsNone(re.search(r"(?:从|From) v\d", text), relative)

        runtime = self.read("docs/runtime/OS_RUNTIME.md")
        self.assertNotIn("v1.11 intentionally", runtime)
        self.assertNotIn("v1.25 keeps", runtime)

    def test_docs_report_current_runtime_and_schema(self) -> None:
        readme = self.read("README.md")
        runtime = self.read("docs/runtime/OS_RUNTIME.md")

        self.assertNotIn("v1.20 beta / Kernel v4.13", readme)
        self.assertIn("Kernel v4.18.0", readme)
        self.assertIn("schema 29", readme)
        self.assertIn("database schema version is `29`", runtime)
        self.assertNotIn("database schema version is `28`", runtime)

    def test_quickstart_uses_current_native_route_advice(self) -> None:
        quickstart = self.read("QUICKSTART.md")

        self.assertIn("native-host-small-verified", quickstart)
        self.assertNotIn("host-codex-spark", quickstart)
        self.assertIn("dispatch native-export", quickstart)
        self.assertIn("same-context-degraded", quickstart)
        self.assertIn("resolved installed Plugin", quickstart)

    def test_install_docs_distinguish_discovery_from_live_hook_execution(self) -> None:
        install = self.read("INSTALL.md")

        self.assertIn("isolated install smoke proves discovery", install)
        self.assertIn("`live-codex` profile proves host Hook execution", install)
        self.assertNotIn("Real hook execution is reserved for the isolated CI smoke", install)

    def test_architecture_docs_name_the_deep_kernel_contracts(self) -> None:
        control_plane = self.read("docs/runtime/CONTROL_PLANE.md")
        runtime = self.read("docs/runtime/OS_RUNTIME.md")

        for marker in ["explicit public API", "Cycle Ledger", "Schema Lifecycle", "Delivery Decision"]:
            self.assertIn(marker, control_plane)
            self.assertIn(marker, runtime)

    def test_changelog_records_stop_ship_compatibility_and_architecture_work(self) -> None:
        changelog = self.read("CHANGELOG.md")

        self.assertIn("real-host compatibility", changelog)
        self.assertIn("Apps/MCP receipt", changelog)
        self.assertIn("explicit public API", changelog)
        self.assertIn("Cycle Ledger", changelog)
        self.assertIn("Schema Lifecycle", changelog)

    def test_fresh_quality_gate_guidance_requires_reviewer_identity(self) -> None:
        readme = self.read("README.md")
        runtime = self.read("docs/runtime/OS_RUNTIME.md")
        skill = self.read("plugins/codex-project-harness/skills/project-runtime/SKILL.md")
        fixture = self.read("docs/runtime/skill-eval-transcript-fixture.txt")

        self.assertIn("`fresh` 不能只靠字符串声明", readme)
        for text in [runtime, skill, fixture]:
            self.assertIn("--reviewer-context fresh", text)
            self.assertIn("--reviewer-session-id", text)
            self.assertIn("--reviewer-attestation-id", text)


if __name__ == "__main__":
    unittest.main()

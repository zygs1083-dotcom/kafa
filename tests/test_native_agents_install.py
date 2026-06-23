import tempfile
import unittest
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
SCRIPTS = REPO_ROOT / "plugins/codex-project-harness/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness_db  # noqa: E402


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


class NativeAgentsInstallTest(unittest.TestCase):
    def test_agents_install_to_custom_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(root, "agents", "install", "--dir", ".codex/native-agents")

            self.assertIn("OK: agents installed", result.stdout)
            self.assertTrue((root / ".codex/native-agents/developer.toml").exists())

    def test_agents_install_does_not_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            developer = root / ".codex/agents/developer.toml"
            original = developer.read_text(encoding="utf-8")
            developer.write_text("custom = true\n", encoding="utf-8")

            blocked = run_harness(root, "agents", "install", check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("agent already exists", blocked.stdout)
            self.assertEqual(developer.read_text(encoding="utf-8"), "custom = true\n")

            run_harness(root, "agents", "install", "--force")
            self.assertEqual(developer.read_text(encoding="utf-8"), original)

    def test_agent_template_schema_rejects_missing_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.toml"
            path.write_text('name = "bad"\ndescription = "Bad"\ncustom = true\n', encoding="utf-8")

            with self.assertRaises(harness_db.HarnessError) as raised:
                harness_db.validate_codex_agent_template(path)

            self.assertIn("missing developer_instructions", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

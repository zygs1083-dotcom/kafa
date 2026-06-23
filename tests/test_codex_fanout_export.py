import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def bootstrap_run(root: Path) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
    return run_harness(root, "dispatch", "plan", "--scope", "Example").stdout.strip().split()[-1]


class CodexFanoutExportTest(unittest.TestCase):
    def test_export_csv_writes_native_spawn_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap_run(root)

            run_harness(root, "dispatch", "export-csv", run_id)

            out_dir = root / ".ai-team/runtime/codex-fanout" / run_id
            input_csv = out_dir / "input.csv"
            instruction = out_dir / "instruction.md"
            output_schema = out_dir / "output_schema.json"
            spawn_config = out_dir / "spawn_config.json"
            self.assertTrue(input_csv.exists())
            self.assertTrue(instruction.exists())
            self.assertTrue(output_schema.exists())
            self.assertTrue(spawn_config.exists())
            with input_csv.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["item_id"], "T1")
            self.assertEqual(rows[0]["target_id"], "UNIT")
            self.assertIn("{task}", instruction.read_text(encoding="utf-8"))
            schema = json.loads(output_schema.read_text(encoding="utf-8"))
            config = json.loads(spawn_config.read_text(encoding="utf-8"))
            self.assertEqual(config["id_column"], "item_id")
            self.assertEqual(config["max_concurrency"], 6)
            self.assertEqual(config["max_runtime_seconds"], 1800)
            self.assertIn("command", schema["required"])
            self.assertIn("branch_name", schema["required"])

    def test_export_rejects_excess_native_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap_run(root)

            result = run_harness(root, "dispatch", "export-csv", run_id, "--max-concurrency", "7", check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("max concurrency", result.stdout)


if __name__ == "__main__":
    unittest.main()

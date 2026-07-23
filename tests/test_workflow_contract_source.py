from __future__ import annotations

import copy
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    REPO_ROOT
    / "plugins"
    / "codex-project-harness"
    / "references"
    / "workflow-contract.json"
)
RENDERER = REPO_ROOT / "tools" / "render_workflow_docs.py"
HARNESS = (
    REPO_ROOT
    / "plugins"
    / "codex-project-harness"
    / "scripts"
    / "harness.py"
)

EXPECTED_TOP_LEVEL_KEYS = {
    "contract_version",
    "authorities",
    "safeguards",
    "routes",
    "advanced_triggers",
    "stages",
    "dependencies",
    "commands",
    "output_labels",
    "handoff_obligations",
    "generated_views",
}
REQUIRED_SAFEGUARDS = {
    "local-only",
    "root-controller-single-writer",
    "native-host-lifecycle",
    "immutable-execution",
    "current-candidate-verification",
    "fail-closed-delivery-gate",
}
REQUIRED_ROUTES = {
    "project-harness",
    "minimal-safe-change",
    "bug-fix-loop",
    "test-first-delivery",
    "independent-quality-gate",
    "harness-audit",
    "project-retrospective",
}
REQUIRED_ADVANCED_TRIGGERS = (
    "parallel-delegation",
    "deep-kernel-review",
    "harness-audit",
    "project-retrospective",
    "live-host-compatibility",
    "release-rehearsal",
)
REQUIRED_GENERATED_VIEWS = {
    "README.md",
    "QUICKSTART.md",
    "plugins/codex-project-harness/skills/project-harness/SKILL.md",
    "plugins/codex-project-harness/docs/TRIGGER_MATRIX.md",
    "examples/full-project-flow.md",
    "docs/runtime/fresh-skill-eval-prompts.md",
}
REQUIRED_DIRECT_DEPENDENCIES = {
    ("delivery-plan", "task-start"),
    ("task-start", "task-submit"),
    ("task-submit", "task-accept"),
    ("controller-verification", "task-accept"),
    ("task-accept", "quality-gate"),
    ("baseline-confirmation", "delivery-readiness"),
    ("quality-gate", "delivery-readiness"),
    ("delivery-readiness", "delivery-record"),
}


def run_renderer(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "tools" / "render_workflow_docs.py"), mode],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def run_contract_command(root: Path, command: str) -> subprocess.CompletedProcess[str]:
    tokens = shlex.split(command)
    if len(tokens) < 5 or tokens[:2] != ["kafa", "project"]:
        raise AssertionError(f"unsupported generated command: {command}")
    domain = tokens[2]
    if tokens[3:5] != ["--repo", "."]:
        raise AssertionError(f"generated command must target the current repo: {command}")
    return subprocess.run(
        [sys.executable, "-B", str(HARNESS), "--root", str(root), domain, *tokens[5:]],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def item_ids(items: object) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        str(item["id"])
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def dependency_pairs(contract: dict[str, Any]) -> set[tuple[str, str]]:
    dependencies = contract.get("dependencies")
    if not isinstance(dependencies, list):
        return set()
    return {
        (str(item["before"]), str(item["after"]))
        for item in dependencies
        if isinstance(item, dict)
        and isinstance(item.get("before"), str)
        and isinstance(item.get("after"), str)
    }


def respects_dependencies(
    sequence: list[str], dependencies: set[tuple[str, str]]
) -> bool:
    positions = {stage: index for index, stage in enumerate(sequence)}
    return all(
        positions[before] < positions[after]
        for before, after in dependencies
        if before in positions and after in positions
    )


def reachable(
    start: str, end: str, dependencies: set[tuple[str, str]]
) -> bool:
    frontier = [start]
    visited: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        for before, after in dependencies:
            if before != current:
                continue
            if after == end:
                return True
            frontier.append(after)
    return False


class WorkflowContractSourceTest(unittest.TestCase):
    maxDiff = None

    def load_contract(self, path: Path = CONTRACT) -> dict[str, Any]:
        self.assertTrue(path.is_file(), f"missing workflow contract: {path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        return document

    def assert_renderer_exists(self) -> None:
        self.assertTrue(RENDERER.is_file(), f"missing workflow renderer: {RENDERER}")

    def make_sandbox(self, destination: Path) -> tuple[Path, dict[str, Any]]:
        self.assert_renderer_exists()
        contract = self.load_contract()

        sandbox_renderer = destination / "tools" / RENDERER.name
        sandbox_renderer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RENDERER, sandbox_renderer)

        sandbox_contract = destination / CONTRACT.relative_to(REPO_ROOT)
        sandbox_contract.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONTRACT, sandbox_contract)

        for view in contract["generated_views"]:
            relative = Path(view["path"])
            source = REPO_ROOT / relative
            target = destination / relative
            self.assertTrue(source.is_file(), f"missing generated view source: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return sandbox_contract, contract

    def test_contract_is_closed_and_covers_required_policy_surfaces(self) -> None:
        contract = self.load_contract()

        self.assertEqual(1, contract.get("contract_version"))
        self.assertEqual(EXPECTED_TOP_LEVEL_KEYS, set(contract))
        self.assertTrue(
            {"root-controller", "native-host"}.issubset(
                item_ids(contract["authorities"])
            )
        )
        self.assertTrue(
            REQUIRED_SAFEGUARDS.issubset(item_ids(contract["safeguards"]))
        )
        self.assertEqual(REQUIRED_ROUTES, item_ids(contract["routes"]))
        self.assertEqual(
            list(REQUIRED_ADVANCED_TRIGGERS),
            [item["id"] for item in contract["advanced_triggers"]],
        )
        for trigger in contract["advanced_triggers"]:
            self.assertEqual({"id", "when", "activates"}, set(trigger))
            self.assertTrue(all(str(value).strip() for value in trigger.values()))
        self.assertTrue(contract["commands"])
        self.assertTrue(contract["output_labels"])
        self.assertTrue(contract["handoff_obligations"])

        serialized = json.dumps(contract["authorities"], ensure_ascii=False).lower()
        for marker in (
            "openspec",
            "sqlite",
            "evaluate_delivery_prerequisites",
            "workflow-contract",
            "native codex/chatgpt",
            "root controller",
        ):
            self.assertIn(marker, serialized, marker)

        views = contract["generated_views"]
        self.assertIsInstance(views, list)
        paths = {str(view["path"]) for view in views}
        self.assertTrue(REQUIRED_GENERATED_VIEWS.issubset(paths))
        block_ids: set[str] = set()
        for view in views:
            self.assertEqual({"path", "blocks"}, set(view))
            self.assertTrue(view["blocks"], view["path"])
            for block in view["blocks"]:
                self.assertIsInstance(block.get("id"), str, view["path"])
                self.assertTrue(block["id"], view["path"])
                self.assertIsInstance(block.get("renderer"), str, block["id"])
                self.assertTrue(block["renderer"], block["id"])
                identity = f"{view['path']}::{block['id']}"
                self.assertNotIn(identity, block_ids)
                block_ids.add(identity)

    def test_contract_is_presentation_only_and_preserves_release_boundaries(self) -> None:
        delivery_source = (
            REPO_ROOT / "plugins/codex-project-harness/core/delivery.py"
        ).read_text(encoding="utf-8")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (
            REPO_ROOT
            / "plugins/codex-project-harness/skills/project-harness/SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("def evaluate_delivery_prerequisites", delivery_source)
        self.assertNotIn("workflow-contract.json", delivery_source)
        for marker in ("v2.0.0-beta.1", "development", "schema 31"):
            self.assertIn(marker, readme, marker)
        self.assertIn("Only the root controller writes Kafa delivery facts", skill)
        self.assertIn("Risk acceptance cannot waive these prerequisites", skill)
        self.assertIn("human-review-required", skill)

    def test_deep_kernel_work_cannot_use_reduced_delegation_to_weaken_ownership(self) -> None:
        contract = self.load_contract()
        triggers = {
            str(item["id"]): item
            for item in contract["advanced_triggers"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        deep = triggers["deep-kernel-review"]
        when = str(deep["when"]).lower()
        for marker in (
            "schema",
            "migration",
            "runtime ownership",
            "trust",
            "delivery gate",
            "security",
            "cross-module authority",
        ):
            self.assertIn(marker, when, marker)
        activates = str(deep["activates"]).lower()
        self.assertIn("root/deep ownership", activates)
        self.assertIn("adversarial review", activates)

        skill = (
            REPO_ROOT
            / "plugins/codex-project-harness/skills/project-harness/SKILL.md"
        ).read_text(encoding="utf-8")
        matrix = (
            REPO_ROOT
            / "plugins/codex-project-harness/references/delegation-matrix.md"
        ).read_text(encoding="utf-8")
        self.assertIn("packet is transfer format only", skill)
        self.assertIn(str(deep["when"]), skill)
        self.assertIn(str(deep["activates"]), skill)
        for marker in (
            "schema/migration/trust/security/permission/concurrency/",
            "data-loss/delivery-gate/public-API/cross-module decisions",
            "human-review-required",
        ):
            self.assertIn(marker, matrix, marker)

    def test_entry_skill_exposes_every_closed_advanced_trigger_without_loading_matrix(self) -> None:
        contract = self.load_contract()
        skill = (
            REPO_ROOT
            / "plugins/codex-project-harness/skills/project-harness/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("### Advanced Trigger Index", skill)
        self.assertIn("docs/TRIGGER_MATRIX.md", skill)
        self.assertIn("Do not load the full matrix by default", skill)
        for trigger in contract["advanced_triggers"]:
            with self.subTest(trigger=trigger["id"]):
                self.assertIn(f"`{trigger['id']}`", skill)
                self.assertIn(trigger["when"], skill)
        deep = next(
            trigger
            for trigger in contract["advanced_triggers"]
            if trigger["id"] == "deep-kernel-review"
        )
        self.assertIn(deep["activates"], skill)

    def test_dependency_dag_allows_submit_and_verify_in_either_order(self) -> None:
        contract = self.load_contract()
        stages = item_ids(contract["stages"])
        dependencies = dependency_pairs(contract)

        self.assertTrue(REQUIRED_DIRECT_DEPENDENCIES.issubset(dependencies))
        self.assertTrue(
            {endpoint for edge in dependencies for endpoint in edge}.issubset(stages)
        )
        self.assertFalse(
            any(reachable(stage, stage, dependencies) for stage in stages),
            "workflow dependencies must be acyclic",
        )
        self.assertFalse(reachable("task-submit", "controller-verification", dependencies))
        self.assertFalse(reachable("controller-verification", "task-submit", dependencies))

        submit_then_verify = [
            "delivery-plan",
            "baseline-confirmation",
            "qualification",
            "task-start",
            "task-submit",
            "controller-verification",
            "task-accept",
            "quality-gate",
            "delivery-readiness",
            "delivery-record",
        ]
        verify_then_submit = [
            "delivery-plan",
            "baseline-confirmation",
            "qualification",
            "controller-verification",
            "task-start",
            "task-submit",
            "task-accept",
            "quality-gate",
            "delivery-readiness",
            "delivery-record",
        ]
        self.assertTrue(respects_dependencies(submit_then_verify, dependencies))
        self.assertTrue(respects_dependencies(verify_then_submit, dependencies))

    def test_dependency_dag_rejects_unsafe_partial_orderings(self) -> None:
        dependencies = dependency_pairs(self.load_contract())
        invalid_sequences = {
            "accept-before-verification": [
                "task-submit",
                "task-accept",
                "controller-verification",
                "quality-gate",
                "delivery-readiness",
                "delivery-record",
            ],
            "gate-before-acceptance": [
                "task-submit",
                "controller-verification",
                "quality-gate",
                "task-accept",
                "delivery-readiness",
                "delivery-record",
            ],
            "record-before-readiness": [
                "task-submit",
                "controller-verification",
                "task-accept",
                "quality-gate",
                "delivery-record",
                "delivery-readiness",
            ],
            "readiness-before-baseline": [
                "delivery-plan",
                "task-start",
                "task-submit",
                "controller-verification",
                "task-accept",
                "quality-gate",
                "delivery-readiness",
                "baseline-confirmation",
                "delivery-record",
            ],
        }
        for name, sequence in invalid_sequences.items():
            with self.subTest(name=name):
                self.assertFalse(respects_dependencies(sequence, dependencies))

    def test_generated_happy_path_uses_plan_ids_and_reaches_the_quality_gate(self) -> None:
        contract = self.load_contract()
        commands = contract["commands"]
        self.assertEqual(
            commands["task-start"],
            "kafa project task --repo . start PATCH-T1",
        )
        self.assertIn("submit PATCH-T1", commands["task-submit"])
        self.assertIn("accept PATCH-T1", commands["task-accept"])
        self.assertIn("--qualification PATCH-Q1", commands["quality-gate"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "patch_contract.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            (root / "test_patch_contract.py").write_text(
                "import unittest\n\n"
                "from patch_contract import add\n\n"
                "class PatchContractTest(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            plan = {
                "version": 1,
                "id": "PATCH",
                "goal": "Keep calculator addition correct",
                "acceptance": "add(2, 3) returns 5",
                "task": "Implement and verify the calculator patch",
                "test": {
                    "kind": "unit",
                    "command": "python3 -B -m unittest test_patch_contract.py",
                },
                "failure_mode": None,
            }
            (root / "delivery-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            results: dict[str, subprocess.CompletedProcess[str]] = {}
            for command_id, command in commands.items():
                results[command_id] = run_contract_command(root, str(command))
                self.assertEqual(
                    0,
                    results[command_id].returncode,
                    f"generated command failed: {command_id}\n"
                    f"stdout:\n{results[command_id].stdout}\n"
                    f"stderr:\n{results[command_id].stderr}",
                )

        applied = json.loads(results["delivery-plan"].stdout)
        self.assertEqual(applied["ids"]["task_id"], "PATCH-T1")
        self.assertEqual(applied["ids"]["qualification_id"], "PATCH-Q1")
        self.assertIn("gate recorded", results["quality-gate"].stdout.lower())

    def test_renderer_check_accepts_current_generated_views(self) -> None:
        self.assert_renderer_exists()
        result = run_renderer(REPO_ROOT, "--check")
        self.assertEqual(
            0,
            result.returncode,
            f"renderer check failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_renderer_write_is_byte_stable_and_uses_utf8_lf(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            sandbox_contract, contract = self.make_sandbox(root)

            first = run_renderer(root, "--write")
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            paths = [root / view["path"] for view in contract["generated_views"]]
            first_bytes = {path: path.read_bytes() for path in paths}
            first_bytes[sandbox_contract] = sandbox_contract.read_bytes()
            for path, payload in first_bytes.items():
                self.assertNotIn(b"\r\n", payload, str(path))
                payload.decode("utf-8")

            second = run_renderer(root, "--write")
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            second_bytes = {path: path.read_bytes() for path in paths}
            second_bytes[sandbox_contract] = sandbox_contract.read_bytes()
            self.assertEqual(first_bytes, second_bytes)

    def test_renderer_check_identifies_drifted_file_and_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _, contract = self.make_sandbox(root)
            write = run_renderer(root, "--write")
            self.assertEqual(0, write.returncode, write.stdout + write.stderr)

            view = contract["generated_views"][0]
            block = view["blocks"][0]
            relative = str(view["path"])
            block_id = str(block["id"])
            target = root / relative
            rendered = target.read_text(encoding="utf-8")
            first_marker = rendered.find(block_id)
            last_marker = rendered.rfind(block_id)
            self.assertGreaterEqual(first_marker, 0, block_id)
            self.assertGreater(last_marker, first_marker, block_id)
            insert_at = rendered.find("\n", first_marker)
            self.assertGreater(insert_at, first_marker, block_id)
            self.assertLess(insert_at, last_marker, block_id)
            target.write_text(
                rendered[: insert_at + 1]
                + "intentional workflow block drift\n"
                + rendered[insert_at + 1 :],
                encoding="utf-8",
                newline="\n",
            )

            check = run_renderer(root, "--check")
            diagnostic = (check.stdout + check.stderr).lower()
            self.assertNotEqual(0, check.returncode)
            self.assertIn(relative.lower(), diagnostic)
            self.assertIn(block_id.lower(), diagnostic)

    def test_renderer_rejects_missing_owner_route_dependency_and_gate(self) -> None:
        Mutation = Callable[[dict[str, Any]], None]

        def remove_id(collection: str, identifier: str) -> Mutation:
            def mutate(contract: dict[str, Any]) -> None:
                contract[collection] = [
                    item
                    for item in contract[collection]
                    if item.get("id") != identifier
                ]

            return mutate

        def remove_dependency(contract: dict[str, Any]) -> None:
            contract["dependencies"] = [
                item
                for item in contract["dependencies"]
                if (item.get("before"), item.get("after"))
                != ("controller-verification", "task-accept")
            ]

        cases: tuple[tuple[str, Mutation, tuple[str, ...]], ...] = (
            (
                "owner",
                remove_id("authorities", "root-controller"),
                ("root-controller", "authorit"),
            ),
            (
                "route",
                remove_id("routes", "minimal-safe-change"),
                ("minimal-safe-change", "route"),
            ),
            (
                "dependency",
                remove_dependency,
                ("controller-verification", "dependenc"),
            ),
            (
                "gate",
                remove_id("stages", "quality-gate"),
                ("quality-gate", "gate"),
            ),
        )

        for name, mutate, diagnostic_markers in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "repo"
                sandbox_contract, contract = self.make_sandbox(root)
                write = run_renderer(root, "--write")
                self.assertEqual(0, write.returncode, write.stdout + write.stderr)

                invalid = copy.deepcopy(contract)
                mutate(invalid)
                sandbox_contract.write_text(
                    json.dumps(invalid, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                check = run_renderer(root, "--check")
                diagnostic = (check.stdout + check.stderr).lower()
                self.assertNotEqual(0, check.returncode)
                self.assertTrue(
                    any(marker.lower() in diagnostic for marker in diagnostic_markers),
                    f"missing actionable {name} diagnostic: {diagnostic}",
                )

    def test_renderer_rejects_missing_or_unknown_advanced_trigger(self) -> None:
        for name in ("missing", "unknown"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "repo"
                sandbox_contract, contract = self.make_sandbox(root)
                write = run_renderer(root, "--write")
                self.assertEqual(0, write.returncode, write.stdout + write.stderr)

                invalid = copy.deepcopy(contract)
                if name == "missing":
                    invalid["advanced_triggers"] = [
                        trigger
                        for trigger in invalid["advanced_triggers"]
                        if trigger["id"] != "harness-audit"
                    ]
                    expected = "harness-audit"
                else:
                    invalid["advanced_triggers"].append(
                        {
                            "id": "unknown-advanced-mode",
                            "when": "unknown policy surface changes",
                            "activates": "unknown evidence",
                        }
                    )
                    expected = "unknown-advanced-mode"
                sandbox_contract.write_text(
                    json.dumps(invalid, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )

                check = run_renderer(root, "--check")
                diagnostic = (check.stdout + check.stderr).lower()
                self.assertNotEqual(0, check.returncode)
                self.assertIn("advanced trigger", diagnostic)
                self.assertIn(expected, diagnostic)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import run_agent_e2e_eval  # noqa: E402

ACTIVE_GUIDANCE = [
    "README.md",
    "INSTALL.md",
    "QUICKSTART.md",
    "docs/runtime/OS_RUNTIME.md",
    "docs/runtime/CONTROL_PLANE.md",
    "docs/runtime/fresh-skill-eval-prompts.md",
    "examples/full-project-flow.md",
    "examples/forward-tests.md",
    "plugins/codex-project-harness/docs/TRIGGER_MATRIX.md",
    "plugins/codex-project-harness/skills/project-harness/SKILL.md",
    "plugins/codex-project-harness/skills/minimal-safe-change/SKILL.md",
    "plugins/codex-project-harness/skills/bug-fix-loop/SKILL.md",
    "plugins/codex-project-harness/skills/test-first-delivery/SKILL.md",
    "plugins/codex-project-harness/skills/independent-quality-gate/SKILL.md",
    "plugins/codex-project-harness/skills/harness-audit/SKILL.md",
    "plugins/codex-project-harness/skills/project-retrospective/SKILL.md",
]

RETAINED_SKILLS = {
    "project-harness",
    "minimal-safe-change",
    "bug-fix-loop",
    "test-first-delivery",
    "independent-quality-gate",
    "harness-audit",
    "project-retrospective",
}

CANONICAL_KERNEL_SPEC = "openspec/specs/local-delivery-kernel/spec.md"
HARDENING_ARCHIVE = (
    "openspec/changes/archive/2026-07-15-local-core-hardening"
)


def shell_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

RETIRED_COMMAND_FRAGMENTS = [
    " connector profile",
    " adapter plan",
    " adapter record",
    " provider start",
    " dispatch plan",
    " dispatch run",
    " dispatch native-",
    " phase project_",
    " scope confirm",
    " session attest",
    " evidence record",
    " test record",
    " checkpoint create",
    " checkpoint export",
    " event export",
    " executor allow-prefix",
    " invariant validate",
    " kernel doctor",
    " task claim",
    " task heartbeat",
    " task recover-stale",
    " task release",
    " task review",
    " task next",
    " task update",
    " task complete",
    "--reviewer-session-id",
    "--reviewer-attestation-id",
    "--trust-anchor",
    "--lease-token",
    "--fence",
]


class DocumentationContractTest(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def active_guidance(self) -> str:
        return "\n".join(self.read(relative) for relative in ACTIVE_GUIDANCE)

    def documented_commands(self) -> str:
        normalized = self.active_guidance().replace("\\\n", " ")
        return "\n".join(
            line.strip().lower()
            for line in normalized.splitlines()
            if "harness.py" in line.lower()
            or line.strip().lower().startswith(("harness ", "$ harness"))
        )

    def test_primary_docs_define_one_local_only_delivery_journey(self) -> None:
        for relative in [
            "README.md",
            "QUICKSTART.md",
            "docs/runtime/OS_RUNTIME.md",
            "docs/runtime/CONTROL_PLANE.md",
            "examples/full-project-flow.md",
        ]:
            text = self.read(relative)
            self.assertIn("local-only", text, relative)
            self.assertIn("Native Codex/ChatGPT", text, relative)
            self.assertIn("root controller", text.lower(), relative)

        readme = self.read("README.md")
        self.assertIn("OpenSpec", readme)
        self.assertIn("verified code handoff", readme)
        self.assertIn("human-review-required", readme)
        self.assertIn("schema 31", readme)

    def test_canonical_path_safety_and_remediation_are_documented(self) -> None:
        guidance = self.read("README.md") + "\n" + self.read("INSTALL.md")
        normalized = " ".join(guidance.split())

        for reason in [
            "invalid-relative-path",
            "unsafe-ancestor",
            "unsafe-target",
            "hard-linked-target",
            "cross-device-ancestor",
            "path-identity-changed",
            "platform-safety-unavailable",
        ]:
            self.assertIn(reason, normalized, reason)
        for marker in [
            "unsafe-project-path",
            "root-level symlink alias",
            "does not sandbox arbitrary verification commands",
            "never automatically follows, rewrites, deletes, or repairs",
            "rollback-incomplete",
            "isolated OS user or container",
        ]:
            self.assertIn(marker, normalized, marker)

    def test_v1_operating_system_plan_is_explicitly_superseded(self) -> None:
        plan = self.read("docs/superpowers/plans/2026-06-22-harness-operating-system.md")
        self.assertIn("Status: Historical / Superseded", plan)
        self.assertIn("do not execute", plan)

    def test_active_guidance_has_no_retired_cli_or_skill_entrypoints(self) -> None:
        guidance = self.active_guidance().lower()
        commands = self.documented_commands()
        for fragment in RETIRED_COMMAND_FRAGMENTS:
            self.assertNotIn(fragment, commands, fragment)
        for retired_skill in [
            "`project-bootstrap`",
            "`project-runtime`",
            "`requirement-baseline`",
            "`team-architecture`",
            "`delivery-readiness`",
        ]:
            self.assertNotIn(retired_skill, guidance, retired_skill)

    def test_current_guidance_uses_immutable_controller_verification(self) -> None:
        for relative in [
            "README.md",
            "QUICKSTART.md",
            "docs/runtime/OS_RUNTIME.md",
            "examples/full-project-flow.md",
            "plugins/codex-project-harness/skills/project-harness/SKILL.md",
            "plugins/codex-project-harness/skills/test-first-delivery/SKILL.md",
        ]:
            text = self.read(relative)
            self.assertIn("verify run", text, relative)
            self.assertIn("immutable", text.lower(), relative)

        quality = self.read(
            "plugins/codex-project-harness/skills/independent-quality-gate/SKILL.md"
        )
        self.assertIn("test-target", quality)
        self.assertIn("verify run", quality)
        self.assertIn("human-review-required", quality)

    def test_schema31_provenance_and_legal_manual_journey_are_documented(self) -> None:
        guidance = "\n".join(
            self.read(relative)
            for relative in (
                "README.md",
                "INSTALL.md",
                "QUICKSTART.md",
                "examples/full-project-flow.md",
                "plugins/codex-project-harness/skills/project-harness/SKILL.md",
                "plugins/codex-project-harness/skills/test-first-delivery/SKILL.md",
                "plugins/codex-project-harness/skills/independent-quality-gate/SKILL.md",
                "plugins/codex-project-harness/skills/harness-audit/SKILL.md",
            )
        )
        for marker in (
            "target_definition_sha256",
            "runtime_executable_sha256",
            "container_image_digest",
            "provenance_status",
            "legacy-incomplete",
            "--pull=never",
        ):
            self.assertIn(marker, guidance, marker)

        flow = self.read("examples/full-project-flow.md")
        for command in (
            "baseline confirm",
            "test-target qualify",
            "--qualification",
            "delivery ready",
        ):
            self.assertIn(command, flow, command)
        self.assertNotIn("harness baseline freeze", flow)
        self.assertIn("schema 31", flow)

        quickstart = self.read("QUICKSTART.md")
        self.assertLess(
            quickstart.index("task accept T1"),
            quickstart.index("gate record"),
            "task acceptance must precede the revision-bound gate",
        )

        eval_prompts = self.read("docs/runtime/fresh-skill-eval-prompts.md")
        for command in (
            "baseline confirm",
            "test-target add/link/qualify",
            "delivery ready",
        ):
            self.assertIn(command, eval_prompts, command)
        self.assertNotIn("baseline freeze", eval_prompts)
        self.assertLess(
            eval_prompts.index("task add/start/submit/accept"),
            eval_prompts.index("gate record"),
        )
        self.assertLess(
            eval_prompts.index("delivery record"),
            eval_prompts.index("validate --delivery"),
        )

        commands = self.documented_commands()
        self.assertNotIn("harness phase ", commands)
        self.assertNotIn("harness.py --root . phase ", commands)

    def test_high_risk_acceptance_cannot_waive_review_prerequisites(self) -> None:
        for relative in [
            "plugins/codex-project-harness/skills/project-harness/SKILL.md",
            "plugins/codex-project-harness/skills/independent-quality-gate/SKILL.md",
            "plugins/codex-project-harness/templates/project/AGENTS.md",
        ]:
            text = self.read(relative)
            self.assertIn("structured current-candidate execution", text, relative)
            self.assertIn("reviewed-local", text, relative)
            self.assertIn("distinct non-empty producer/reviewer contexts", text, relative)
            self.assertIn("Risk acceptance cannot waive these prerequisites", text, relative)

    def test_project_harness_defines_a_bounded_host_delegation_matrix(self) -> None:
        skill = self.read("plugins/codex-project-harness/skills/project-harness/SKILL.md")
        reference_path = "plugins/codex-project-harness/references/delegation-matrix.md"
        reference = self.read(reference_path)

        for marker in [
            "Delegation Matrix",
            "| Task |",
            "Acceptance",
            "Depends On",
            "Exclusive Files",
            "Shared Files",
            "Targeted Test",
            "Integration Test",
            "Capability Hint",
            "Context Budget",
            "Output Budget",
            "Latency Budget",
            "Escalation",
        ]:
            self.assertIn(marker, reference, marker)
        self.assertIn("references/delegation-matrix.md", skill)
        self.assertIn("Native Host maps capability hints to actual models", reference)
        self.assertIn("Parallelism is a wall-clock optimization, not a token optimization", reference)
        self.assertIn("batch them into one producer", reference)
        self.assertLessEqual(len(skill.encode("utf-8")), 12_800)
        self.assertLessEqual(
            len(skill.encode("utf-8")) + len(reference.encode("utf-8")),
            16_000,
        )
        for routing_contract in [
            "Default one producer",
            "Use two or three only",
            "waves capped at three",
            "UTF-8 bytes",
            "<=4,000 UTF-8 bytes",
            "required saving",
            "no latency SLA means no fan-out",
            "never split work only to satisfy this target",
            "model identity is unavailable",
        ]:
            self.assertIn(routing_contract, reference, routing_contract)

    def test_plugin_inventory_is_exact_and_documented(self) -> None:
        skills = {
            path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir()
        }
        hooks = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )["hooks"]
        templates = {
            path.name
            for path in (PLUGIN_ROOT / "templates" / "agents").glob("*.toml")
        }

        self.assertEqual(skills, RETAINED_SKILLS)
        self.assertEqual(set(hooks), {"SessionStart", "SubagentStart", "Stop"})
        self.assertEqual(
            templates, {"architect.toml", "developer.toml", "qa-reviewer.toml"}
        )

        install = self.read("INSTALL.md")
        self.assertIn("seven Skills", install)
        self.assertIn("three Hooks", install)
        self.assertIn("three agent templates", install)

    def test_skill_eval_fixture_enforces_the_local_contract(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts" / "run_skill_eval.py"),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local-only skill eval transcript passed", result.stdout)

    def test_skill_eval_help_does_not_execute_the_fixture(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts" / "run_skill_eval.py"),
                "--help",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertNotIn("transcript passed", result.stdout.lower())

    def test_skill_eval_rejects_failed_host_command_even_with_all_markers(self) -> None:
        fixture = REPO_ROOT / "docs/runtime/skill-eval-transcript-fixture.txt"
        with tempfile.TemporaryDirectory() as temp:
            producer = Path(temp) / "marker producer.py"
            producer.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "print(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
                "raise SystemExit(int(sys.argv[2]))\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["CODEX_EVAL_CMD"] = shell_command(
                [sys.executable, str(producer), str(fixture), "7"]
            )
            result = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts/run_skill_eval.py")],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("host skill eval command failed", result.stdout.lower())
        self.assertIn("returncode=7", result.stdout.lower())
        self.assertNotIn("missing skill eval marker", result.stdout.lower())

    def test_skill_eval_rejects_reversed_workflow_markers(self) -> None:
        fixture = REPO_ROOT / "docs/runtime/skill-eval-transcript-fixture.txt"
        with tempfile.TemporaryDirectory() as temp:
            reversed_transcript = Path(temp) / "reversed transcript.txt"
            reversed_transcript.write_text(
                "\n".join(reversed(fixture.read_text(encoding="utf-8").splitlines())) + "\n",
                encoding="utf-8",
            )
            producer = Path(temp) / "marker producer.py"
            producer.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "print(Path(sys.argv[1]).read_text(encoding='utf-8'))\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            producer_command = [
                sys.executable,
                str(producer),
                str(reversed_transcript),
            ]
            env["CODEX_EVAL_CMD"] = shell_command(producer_command)
            result = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts/run_skill_eval.py")],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("out-of-order skill eval marker", result.stdout.lower())
        self.assertNotIn("host skill eval command failed", result.stdout.lower())
        self.assertNotIn("missing skill eval marker", result.stdout.lower())

    def test_e2e_example_reports_only_current_local_scenarios(self) -> None:
        report = json.loads(
            self.read("docs/runtime/agent-e2e-eval-example.json")
        )
        names = {scenario["name"] for scenario in report["scenarios"]}
        categories = {scenario["category"] for scenario in report["scenarios"]}

        self.assertEqual(report["mode"], "stability")
        self.assertEqual(report["report_version"], 1)
        self.assertEqual(report["evidence_scope"], "deterministic-local-runtime")
        self.assertEqual(report["summary"]["scenario_count"], 11)
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 0)
        self.assertEqual(report["summary"]["false_pass_count"], 0)
        self.assertNotIn("task_once_completion_rate", report["summary"])
        self.assertNotIn("retry_count", report["summary"])
        self.assertEqual(len(report["evaluation_source"]["workspace_sha256"]), 64)
        self.assertIn("high_risk_requires_human_review", names)
        self.assertIn("schema27_to_active_migration_and_rollback", names)
        self.assertFalse(categories & {"connector", "provider", "host-codex"})

    def test_migration_recovery_guidance_distinguishes_safe_and_incomplete_cleanup(
        self,
    ) -> None:
        spec = self.read(CANONICAL_KERNEL_SPEC)
        public_guidance = "\n".join(
            [
                self.read("README.md"),
                self.read("INSTALL.md"),
            ]
        )

        for text in (spec, public_guidance):
            self.assertIn("recovery-required", text)
            self.assertIn("rollback-incomplete", text)
            self.assertIn("verified complete rollback", text)
        self.assertIn("hard process exit", spec)
        self.assertIn("must not remove", public_guidance)
        self.assertNotIn(
            "the diagnostic sentinel is removed for a handled failure, and later normal operations can proceed",
            spec,
        )

    def test_project_state_projection_is_database_deterministic(self) -> None:
        design = self.read(f"{HARDENING_ARCHIVE}/design.md")
        spec = self.read(CANONICAL_KERNEL_SPEC)
        public_guidance = "\n".join(
            [
                self.read("README.md"),
                self.read("INSTALL.md"),
            ]
        )

        for text in (design, spec, public_guidance):
            self.assertIn("project.updated_at", text)
            self.assertIn("replace rather than merge", text)

    def test_final_hardening_followups_are_explicitly_fail_closed(self) -> None:
        contract = "\n".join(
            [
                self.read(f"{HARDENING_ARCHIVE}/design.md"),
                self.read(CANONICAL_KERNEL_SPEC),
                self.read("README.md"),
                self.read("INSTALL.md"),
            ]
        )
        for marker in (
            "callback self-report",
            "BaseException",
            "GIT_WORK_TREE",
            "sqlite_sequence",
            "private Git-backed snapshot",
        ):
            self.assertIn(marker, contract)

    def test_candidate_identity_distinguishes_source_from_dependency_environments(
        self,
    ) -> None:
        design = self.read(f"{HARDENING_ARCHIVE}/design.md")
        spec = self.read(CANONICAL_KERNEL_SPEC)
        public_guidance = "\n".join(
            [
                self.read("README.md"),
                self.read("INSTALL.md"),
            ]
        )

        for text in (design, spec, public_guidance):
            self.assertIn("top-level dependency/tool environment", text)
            self.assertIn(".venv", text)
            self.assertIn("node_modules", text)
            self.assertIn("lockfile", text)
            self.assertIn("exact generated projection", text)
            self.assertIn(".gitignore", text)
        self.assertIn("ignored runtime source", spec)

    def test_native_report_contract_binds_profile_telemetry_and_producer_mapping(
        self,
    ) -> None:
        contract = "\n".join(
            [
                self.read(f"{HARDENING_ARCHIVE}/design.md"),
                self.read(CANONICAL_KERNEL_SPEC),
            ]
        )
        for marker in (
            "evidence_scope",
            "matrix.profile",
            "positive finite",
            "current Native Codex binary",
            "task-to-scope",
            "Connector/Host",
            "report_version=1",
            "path-discovery",
            "unmerged",
        ):
            self.assertIn(marker, contract)

    def test_persistent_native_host_evidence_is_compact_and_truthful(self) -> None:
        live = json.loads(self.read("docs/runtime/native-codex-live-eval.json"))
        parallel = json.loads(self.read("docs/runtime/native-codex-parallel-eval.json"))

        for name, report in [("live", live), ("parallel", parallel)]:
            self.assertEqual(report["live_status"], "passed", name)
            self.assertFalse(report["live_skipped"], name)
            self.assertGreater(report["token_count"], 0, name)
            self.assertEqual(
                report["token_count"],
                report["token_usage"]["input_tokens"]
                + report["token_usage"]["output_tokens"],
                name,
            )
            self.assertGreater(report["agent_runtime_seconds"], 0, name)
            self.assertIsNone(report["estimated_cost"], name)
            self.assertEqual(len(report["evaluation_source"]["workspace_sha256"]), 64, name)
            self.assertIn("plugins/", report["evaluation_source"]["source_scope"], name)
            self.assertEqual(
                run_agent_e2e_eval.persistent_report_consistency_errors(report),
                [],
                name,
            )
            self.assertEqual(
                report["native_host"]["trust"],
                "local-capability-only-not-delivery-provenance",
                name,
            )
            self.assertNotEqual(report["native_host"]["sha256"], "0" * 64, name)
            serialized = json.dumps(report, ensure_ascii=False)
            for verbose in [
                "native_stdout_tail",
                "native_stderr_tail",
                "controller_verify_output",
                "stdout_tail",
                "stderr_tail",
            ]:
                self.assertNotIn(verbose, serialized, f"{name}: {verbose}")

        details = parallel["scenarios"][0]["details"]
        current_source = run_agent_e2e_eval.evaluation_source_identity()
        for field in ["workspace_sha256", "source_scope"]:
            self.assertEqual(live["evaluation_source"][field], current_source[field], field)
            self.assertEqual(parallel["evaluation_source"][field], current_source[field], field)
        for field in [
            "git_head",
            "git_dirty",
            "status_sha256",
            "status_entry_count",
        ]:
            self.assertEqual(
                live["evaluation_source"][field],
                parallel["evaluation_source"][field],
                field,
            )
        self.assertEqual(
            live["evaluation_source"]["workspace_sha256"],
            parallel["evaluation_source"]["workspace_sha256"],
        )
        self.assertEqual(
            live["evaluation_source"]["status_sha256"],
            parallel["evaluation_source"]["status_sha256"],
        )
        self.assertEqual(details["producer_count"], 2)
        self.assertEqual(live["scenarios"][0]["details"]["workload_units"], 1)
        self.assertEqual(details["workload_units"], 2)
        self.assertEqual(
            live["scenarios"][0]["details"]["workload_unit_sha256"],
            details["workload_unit_sha256"],
        )
        self.assertEqual(details["native_token_scope"], "native-producers-only")
        self.assertGreater(details["producer_overlap_seconds"], 0)
        self.assertEqual(details["changed_files"], ["alpha.py", "beta.py"])
        self.assertEqual(details["scope_conflicts"], {})
        self.assertEqual(details["combined_verify_returncode"], 0)
        self.assertTrue(details["producer_attribution_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertEqual(details["overlap_policy"], "block-parallel-on-declared-overlap")
        self.assertEqual(
            details["scope_enforcement"],
            "isolated-producer-workspaces-plus-exact-diff-integration",
        )

    def test_superseded_runtime_records_cannot_be_mistaken_for_current_guidance(self) -> None:
        for relative in [
            "docs/runtime/APPS_MCP_RECEIPT_ADR.md",
            "docs/runtime/NATIVE_CODEX_RUNTIME_ADR.md",
        ]:
            text = self.read(relative)
            self.assertIn("Status: Superseded", text, relative)
            self.assertIn("local-core-slimming", text, relative)
            self.assertLess(len(text.splitlines()), 50, relative)
            self.assertNotIn('"receipt_version"', text, relative)
            self.assertNotIn("legacy-direct", text, relative)

        schema29 = self.read("docs/runtime/SCHEMA_29_MIGRATION.md")
        self.assertIn("Historical migration record", schema29)
        self.assertIn("schema 30", schema29)

    def test_architecture_docs_name_the_deep_kernel_contracts(self) -> None:
        for relative in [
            "docs/runtime/CONTROL_PLANE.md",
            "docs/runtime/OS_RUNTIME.md",
        ]:
            text = self.read(relative)
            for marker in [
                "explicit public API",
                "Cycle Ledger",
                "Schema Lifecycle",
                "Delivery Decision",
            ]:
                self.assertIn(marker, text, f"{relative}: {marker}")


if __name__ == "__main__":
    unittest.main()

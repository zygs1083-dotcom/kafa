from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import ast
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py"
sys.path.insert(0, str(EVAL.parent))
import run_agent_e2e_eval  # noqa: E402


def run_eval(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    result = run_eval_process(*args, env=env)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def run_eval_process(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.setdefault("N8N_MCP_ACCESS_TOKEN", "ambient-test-secret")
    if env is not None:
        command_env.update(env)
        if env.get("HARNESS_E2E_CODEX_BIN"):
            command_env.setdefault("HARNESS_E2E_ALLOW_CODEX_BIN_OVERRIDE", "1")
    return subprocess.run([sys.executable, str(EVAL), *args], text=True, capture_output=True, check=False, env=command_env)


def make_fake_codex(
    root: Path,
    *,
    tamper_state: bool = False,
    tamper_attribution: bool = False,
) -> Path:
    script = root / "fake_codex.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        f"TAMPER_STATE = {tamper_state!r}\n"
        f"TAMPER_ATTRIBUTION = {tamper_attribution!r}\n"
        "args = sys.argv[1:]\n"
        "if os.environ.get('N8N_MCP_ACCESS_TOKEN'):\n"
        "    print('ambient secret leaked to configured Codex binary', file=sys.stderr)\n"
        "    raise SystemExit(91)\n"
        "if args == ['login', 'status']:\n"
        "    print('Logged in using test fixture')\n"
        "    raise SystemExit(0)\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 0.143.0')\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'exec':\n"
        "    work = pathlib.Path(args[args.index('--cd') + 1])\n"
        "    prompt = args[-1]\n"
        "    if 'ALPHA-PRODUCER' in prompt:\n"
        "        time.sleep(0.15)\n"
        "        (work / 'alpha.py').write_text('VALUE = \\\"after\\\"\\n', encoding='utf-8')\n"
        "        if TAMPER_ATTRIBUTION:\n"
        "            (work / 'beta.py').write_text('VALUE = \\\"tampered\\\"\\n', encoding='utf-8')\n"
        "        token_count = 600\n"
        "    elif 'BETA-PRODUCER' in prompt:\n"
        "        time.sleep(0.15)\n"
        "        (work / 'beta.py').write_text('VALUE = \\\"after\\\"\\n', encoding='utf-8')\n"
        "        token_count = 700\n"
        "    else:\n"
        "        (work / 'candidate.py').write_text('VALUE = \\\"after\\\"\\n', encoding='utf-8')\n"
        "        token_count = 1234\n"
        "    if TAMPER_STATE:\n"
        "        state = work / '.ai-team/state/harness.db'\n"
        "        state.parent.mkdir(parents=True, exist_ok=True)\n"
        "        state.write_text('tampered', encoding='utf-8')\n"
        "    if '--output-last-message' in args:\n"
        "        out = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
        "        out.parent.mkdir(parents=True, exist_ok=True)\n"
        "        out.write_text('edited candidate.py and ran the requested test\\n', encoding='utf-8')\n"
        "    print(json.dumps({'type': 'turn.completed', 'usage': {\n"
        "        'input_tokens': token_count - 10,\n"
        "        'cached_input_tokens': 100,\n"
        "        'output_tokens': 10,\n"
        "        'reasoning_output_tokens': 1,\n"
        "    }}))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = root / "codex.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = root / "codex"
        launcher.write_text(f'#!{sys.executable}\nexec(open({str(script)!r}).read())\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


class AgentE2EEvalTest(unittest.TestCase):
    def test_evaluation_source_identity_excludes_generated_python_caches(self) -> None:
        self.assertTrue(
            run_agent_e2e_eval._is_evaluation_cache_path(
                "plugins/codex-project-harness/scripts/__pycache__/eval.pyc"
            )
        )
        self.assertTrue(
            run_agent_e2e_eval._is_evaluation_cache_path("tests/.pytest_cache/state")
        )
        self.assertFalse(
            run_agent_e2e_eval._is_evaluation_cache_path("tests/test_agent_e2e_eval.py")
        )

    def test_persisted_report_keeps_historical_git_state_but_requires_current_source(self) -> None:
        report = json.loads(
            (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_text(
                encoding="utf-8"
            )
        )
        historical_checkout = {
            **report["evaluation_source"],
            "git_head": "f" * 40,
            "git_dirty": False,
            "status_sha256": "e" * 64,
            "status_entry_count": 0,
        }
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=historical_checkout,
        ):
            strict_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
            )
            persisted_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_git_state=False,
            )

        self.assertTrue(any("current checkout" in error for error in strict_errors))
        self.assertEqual(persisted_errors, [])

        changed_source = {
            **historical_checkout,
            "workspace_sha256": "0" * 64,
        }
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=changed_source,
        ):
            changed_source_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_git_state=False,
            )
        self.assertTrue(
            any("workspace_sha256" in error for error in changed_source_errors)
        )

        zero_head = json.loads(json.dumps(report))
        zero_head["evaluation_source"]["git_head"] = "0" * 40
        zero_head_errors = run_agent_e2e_eval.report_consistency_errors(
            zero_head,
            require_current_binary=False,
            require_current_git_state=False,
        )
        self.assertTrue(any("git_head" in error for error in zero_head_errors))

        for generated_at in (None, "not-a-timestamp", "2026-07-12T06:00:00"):
            with self.subTest(generated_at=generated_at):
                invalid_time = json.loads(json.dumps(report))
                invalid_time["evaluation_source"]["generated_at"] = generated_at
                invalid_time_errors = run_agent_e2e_eval.report_consistency_errors(
                    invalid_time,
                    require_current_binary=False,
                    require_current_git_state=False,
                )
                self.assertTrue(
                    any("generated_at" in error for error in invalid_time_errors)
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(invalid_time))

    def test_fixture_eval_runs_six_real_local_kernel_scenarios(self) -> None:
        report = run_eval("--mode", "fixture")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "fixture")
        self.assertEqual(len(report["evaluation_source"]["workspace_sha256"]), 64)
        self.assertEqual(len(report["evaluation_source"]["status_sha256"]), 64)
        self.assertTrue(report["evaluation_source"]["generated_at"])
        self.assertIn("matrix", report)
        self.assertEqual(report["matrix"]["profile"], "fixture")
        self.assertEqual(report["evidence_scope"], "deterministic-local-runtime")
        self.assertEqual(summary["scenario_count"], 6)
        self.assertEqual(summary["passed_count"], 6)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["expected_human_review_required_count"], 1)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertNotIn("task_once_completion_rate", summary)
        self.assertNotIn("retry_count", summary)
        self.assertIsNone(report["token_count"])
        self.assertIsNone(report["agent_runtime_seconds"])
        self.assertEqual(
            set(scenarios),
            {
                "fresh_local_install_and_init",
                "quickstart_stops_before_independent_review",
                "current_candidate_supersedes_stale_validation",
                "manual_evidence_cannot_satisfy_delivery",
                "open_high_finding_blocks_delivery",
                "high_risk_requires_human_review",
            },
        )
        self.assertTrue(all(scenario["pass"] for scenario in scenarios.values()))

    def test_success_scenarios_do_not_replace_delivery_validator(self) -> None:
        tree = ast.parse(EVAL.read_text(encoding="utf-8"))
        assignments = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "validate_runtime":
                    assignments.append(node.lineno)

        self.assertEqual(assignments, [], f"release-critical validate_runtime replaced at lines {assignments}")

    def test_stability_eval_runs_matrix_scenarios(self) -> None:
        report = run_eval("--mode", "stability")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "stability")
        self.assertEqual(report["matrix"]["profile"], "stability")
        self.assertTrue(report["matrix"]["sqlite_stress"])
        self.assertEqual(summary["scenario_count"], 11)
        self.assertEqual(summary["passed_count"], 11)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["expected_human_review_required_count"], 1)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(
            set(scenarios),
            {
                "fresh_local_install_and_init",
                "quickstart_stops_before_independent_review",
                "current_candidate_supersedes_stale_validation",
                "manual_evidence_cannot_satisfy_delivery",
                "open_high_finding_blocks_delivery",
                "high_risk_requires_human_review",
                "structured_and_no_network_policy_fail_closed",
                "cycle_isolation",
                "sqlite_contention_stress",
                "schema27_29_migration_and_rollback",
                "installed_plugin_surface",
            },
        )
        self.assertTrue(scenarios["sqlite_contention_stress"]["pass"], scenarios["sqlite_contention_stress"]["details"])
        self.assertEqual(scenarios["sqlite_contention_stress"]["details"]["sqlite_lock_error_count"], 0)
        self.assertTrue(scenarios["schema27_29_migration_and_rollback"]["details"]["rollback_observed"])
        self.assertEqual(scenarios["installed_plugin_surface"]["details"]["skill_count"], 7)

    def test_eval_source_contains_no_retired_provider_or_connector_scenarios(self) -> None:
        source = EVAL.read_text(encoding="utf-8")

        for marker in (
            "HostCodexProvider",
            "scenario_host_codex_fake_sdk_e2e",
            "connector_mock",
            "scenario_connector",
            "spark_policy",
            "provider_crash_recovery",
            "native_receipt",
        ):
            self.assertNotIn(marker, source)

    def test_live_codex_without_enable_is_not_run_and_fails_explicit_profile(self) -> None:
        result = run_eval_process("--mode", "live-codex", env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["mode"], "live-codex")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["live_status"], "not-run")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 1)
        self.assertTrue(all(not scenario["pass"] for scenario in report["scenarios"]))
        self.assertEqual({scenario["name"] for scenario in report["scenarios"]}, {"native_codex_edit_and_controller_verify"})
        self.assertIn("HARNESS_E2E_ENABLE_LIVE_CODEX", "; ".join(report["matrix"]["live_skipped_reasons"]))

    def test_live_codex_enabled_without_authenticated_codex_is_blocked(self) -> None:
        result = run_eval_process(
            "--mode",
            "live-codex",
            env={
                "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                "HARNESS_E2E_CODEX_BIN": sys.executable,
            },
        )
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["live_skipped"])
        self.assertEqual(report["live_status"], "blocked")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertGreaterEqual(report["summary"]["failed_count"], 1)
        self.assertTrue(all(not scenario["skip_reason"] for scenario in report["scenarios"]))
        self.assertTrue(all(scenario["details"]["capability_status"] == "blocked" for scenario in report["scenarios"]))

    def test_live_codex_environment_copies_only_auth_into_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            target = Path(temp) / "isolated"
            source.mkdir()
            (source / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            (source / "config.toml").write_text("model = 'fixture'\n", encoding="utf-8")
            (source / "plugins").mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(source),
                    "HARNESS_CODEX_MODEL_POLICY": "retired-policy-must-not-leak",
                    "N8N_MCP_ACCESS_TOKEN": "ambient-secret-must-not-leak",
                },
            ):
                env = run_agent_e2e_eval.isolated_live_codex_environment(target)

            self.assertEqual(Path(env["CODEX_HOME"]), target)
            self.assertEqual(Path(env["HOME"]), target)
            self.assertEqual({path.name for path in target.iterdir()}, {"auth.json"})
            self.assertEqual((target / "auth.json").read_text(encoding="utf-8"), '{"fixture": true}\n')
            self.assertIsNone(env.get("HARNESS_CODEX_MODEL_POLICY"))
            self.assertIsNone(env.get("N8N_MCP_ACCESS_TOKEN"))

    def test_live_codex_profile_wiring_edits_then_controller_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(report["live_skipped"])
        self.assertEqual(report["live_status"], "passed")
        self.assertEqual(report["summary"]["scenario_count"], 1)
        self.assertEqual(report["summary"]["passed_count"], 1)
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["name"], "native_codex_edit_and_controller_verify")
        self.assertEqual(scenario["details"]["changed_files"], ["candidate.py"])
        self.assertEqual(scenario["details"]["exclusive_files"], ["candidate.py"])
        self.assertEqual(scenario["details"]["workload_units"], 1)
        self.assertEqual(scenario["details"]["native_token_scope"], "native-producers-only")
        self.assertTrue(scenario["details"]["test_file_unchanged"])
        self.assertEqual(scenario["details"]["controller_verify_returncode"], 0)
        self.assertEqual(scenario["details"]["execution_count"], 1)
        self.assertEqual(scenario["details"]["validation_count"], 1)
        self.assertEqual(scenario["details"]["task_status"], "submitted")
        self.assertTrue(scenario["details"]["provider_surface_absent"])
        self.assertEqual(scenario["details"]["retired_host_tables"], [])
        self.assertTrue(scenario["details"]["producer_scope_valid"])
        self.assertTrue(scenario["details"]["controller_state_unchanged_during_native"])
        self.assertEqual(scenario["details"]["integrated_files"], ["candidate.py"])
        self.assertEqual(report["token_count"], 1234)
        self.assertEqual(report["token_usage"]["input_tokens"], 1224)
        self.assertEqual(report["token_usage"]["output_tokens"], 10)
        self.assertGreater(report["agent_runtime_seconds"], 0)
        self.assertIsNone(report["estimated_cost"])
        self.assertEqual(
            report["native_host"]["trust"],
            "local-capability-only-not-delivery-provenance",
        )
        self.assertEqual(report["native_host"]["source"], "explicit-test-override")
        self.assertEqual(len(report["native_host"]["sha256"]), 64)
        self.assertEqual(
            run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
            ),
            [],
        )

        inconsistent = json.loads(json.dumps(report))
        inconsistent["summary"]["scenario_count"] = 99
        inconsistent["summary"]["duration_seconds"] = -1
        inconsistent["live_status"] = "blocked"
        inconsistent["token_count"] += 1
        inconsistent["evaluation_source"]["workspace_sha256"] = "0" * 64
        inconsistent["evaluation_source"]["status_sha256"] = "1" * 64
        inconsistent["native_host"]["resolved_path"] = str(EVAL)
        inconsistent["native_host"]["sha256"] = "1" * 64
        inconsistent_details = inconsistent["scenarios"][0]["details"]
        inconsistent_details["integrated_files"] = []
        inconsistent_details["native_token_source"] = "assistant-text"
        errors = run_agent_e2e_eval.report_consistency_errors(inconsistent)
        self.assertTrue(any("scenario_count" in error for error in errors))
        self.assertTrue(any("summary duration_seconds" in error for error in errors))
        self.assertTrue(any("live_status" in error for error in errors))
        self.assertTrue(any("top-level token_count" in error for error in errors))
        self.assertTrue(any("nonzero SHA-256" in error for error in errors))
        self.assertTrue(any("current checkout" in error for error in errors))
        self.assertTrue(any("resolved binary" in error for error in errors))
        self.assertTrue(any("integrated_files" in error for error in errors))
        self.assertTrue(any("native_token_source" in error for error in errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(inconsistent))

        empty_scope = json.loads(json.dumps(report))
        empty_scope["scenarios"][0]["details"]["exclusive_files"] = []
        empty_scope_errors = run_agent_e2e_eval.report_consistency_errors(empty_scope)
        self.assertTrue(any("exclusive_files is empty" in error for error in empty_scope_errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(empty_scope))

        missing_binary = json.loads(json.dumps(report))
        missing_binary["native_host"]["resolved_path"] = "/definitely/missing/kafa-codex"
        missing_binary_errors = run_agent_e2e_eval.report_consistency_errors(missing_binary)
        self.assertTrue(any("resolved binary is unavailable" in error for error in missing_binary_errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(missing_binary))
        serialized = json.dumps(report, ensure_ascii=False)
        for key in run_agent_e2e_eval.VERBOSE_NATIVE_OUTPUT_KEYS:
            self.assertNotIn(key, serialized)

    def test_native_usage_parser_accepts_only_structured_turn_completion(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"text": "tokens used\\n999"}}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 13400,
                            "cached_input_tokens": 2000,
                            "output_tokens": 66,
                            "reasoning_output_tokens": 12,
                        },
                    }
                ),
            ]
        )

        usage = run_agent_e2e_eval.parse_native_usage_jsonl(output)

        self.assertEqual(usage["token_count"], 13466)
        self.assertEqual(usage["cached_input_tokens"], 2000)
        self.assertIsNone(run_agent_e2e_eval.parse_native_usage_jsonl("tokens used\n999\n"))
        self.assertIsNone(
            run_agent_e2e_eval.parse_native_usage_jsonl(output + "\n" + output.splitlines()[-1])
        )
        invalid_reasoning = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 2,
                },
            }
        )
        self.assertIsNone(run_agent_e2e_eval.parse_native_usage_jsonl(invalid_reasoning))

    def test_compact_evidence_report_removes_only_verbose_native_output(self) -> None:
        report = {
            "token_count": 10,
            "details": {
                "native_stdout_tail": "verbose",
                "native_stderr_tail": "verbose",
                "controller_verify_output": "verbose",
                "result": "pass",
                "producers": [
                    {"stdout_tail": "verbose", "stderr_tail": "verbose", "token_count": 10}
                ],
            },
        }

        compact = run_agent_e2e_eval.compact_evidence_report(report)

        self.assertEqual(compact["token_count"], 10)
        self.assertEqual(compact["details"]["result"], "pass")
        self.assertEqual(compact["details"]["producers"], [{"token_count": 10}])
        self.assertFalse(run_agent_e2e_eval.VERBOSE_NATIVE_OUTPUT_KEYS & set(compact["details"]))

    def test_live_codex_has_no_permanent_repository_profile_skip(self) -> None:
        source = EVAL.read_text(encoding="utf-8")

        self.assertNotIn("no repository-local live profile is configured", source)

    def test_live_parallel_scope_guard_rejects_shared_write_paths(self) -> None:
        conflicts = run_agent_e2e_eval.live_eval_scope_conflicts(
            [
                {"task": "A", "exclusive_files": ["shared.py", "alpha.py"]},
                {"task": "B", "exclusive_files": ["shared.py", "beta.py"]},
            ]
        )

        self.assertEqual(conflicts, {"shared.py": ["A", "B"]})
        aliases = run_agent_e2e_eval.live_eval_scope_conflicts(
            [
                {"task": "A", "exclusive_files": ["alpha.py"]},
                {"task": "B", "exclusive_files": ["./alpha.py"]},
            ]
        )
        invalid = run_agent_e2e_eval.live_eval_scope_conflicts(
            [{"task": "A", "exclusive_files": ["../escape.py"]}]
        )
        self.assertEqual(aliases, {"alpha.py": ["A", "B"]})
        self.assertEqual(invalid, {"<invalid:../escape.py>": ["A"]})

    def test_live_parallel_without_enable_is_not_run(self) -> None:
        result = run_eval_process(
            "--mode",
            "live-codex-parallel",
            env={"HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": ""},
        )
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["mode"], "live-codex-parallel")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["live_status"], "not-run")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 1)
        self.assertEqual(
            {scenario["name"] for scenario in report["scenarios"]},
            {"native_codex_two_producer_integration"},
        )

    def test_live_parallel_profile_runs_two_disjoint_producers_and_combined_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex-parallel",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["live_status"], "passed")
        self.assertEqual(report["token_count"], 1300)
        self.assertGreater(report["agent_runtime_seconds"], 0)
        scenario = report["scenarios"][0]
        details = scenario["details"]
        self.assertEqual(details["producer_count"], 2)
        self.assertGreater(details["producer_overlap_seconds"], 0)
        self.assertEqual(details["workload_units"], 2)
        self.assertEqual(details["native_token_scope"], "native-producers-only")
        self.assertEqual(
            details["producer_overlap_seconds"],
            round(
                min(producer["finished_offset_seconds"] for producer in details["producers"])
                - max(producer["started_offset_seconds"] for producer in details["producers"]),
                6,
            ),
        )
        self.assertEqual(details["changed_files"], ["alpha.py", "beta.py"])
        self.assertEqual(details["integrated_files"], ["alpha.py", "beta.py"])
        self.assertTrue(details["producer_attribution_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertTrue(details["test_files_unchanged"])
        self.assertEqual(details["targeted_verify_returncodes"], {"LIVE-ALPHA": 0, "LIVE-BETA": 0})
        self.assertEqual(details["combined_verify_returncode"], 0)
        self.assertEqual(
            details["task_statuses"],
            {"LIVE-INTEGRATE": "submitted", "LIVE-P1": "accepted", "LIVE-P2": "accepted"},
        )
        self.assertEqual(details["scope_conflicts"], {})
        self.assertEqual(details["overlap_policy"], "block-parallel-on-declared-overlap")
        self.assertEqual(
            details["scope_enforcement"],
            "isolated-producer-workspaces-plus-exact-diff-integration",
        )
        self.assertEqual(details["retired_host_tables"], [])
        self.assertEqual(
            run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
            ),
            [],
        )

        inconsistent = json.loads(json.dumps(report))
        inconsistent_details = inconsistent["scenarios"][0]["details"]
        producers = inconsistent_details["producers"]
        producers[1]["exclusive_files"] = ["alpha.py"]
        producers[0]["returncode"] = 1
        producers[0]["test_file_unchanged"] = False
        producers[0]["runtime_seconds"] = 999
        producers[0]["token_source"] = "assistant-text"
        inconsistent_details["producer_count"] = 3
        inconsistent_details["producer_overlap_seconds"] += 0.25
        inconsistent_details["integrated_files"] = ["alpha.py"]
        errors = run_agent_e2e_eval.report_consistency_errors(inconsistent)
        self.assertTrue(any("scope_conflicts" in error for error in errors))
        self.assertTrue(any("producer_attribution_valid" in error for error in errors))
        self.assertTrue(any("producer_count" in error for error in errors))
        self.assertTrue(any("producer_overlap_seconds" in error for error in errors))
        self.assertTrue(any("integrated_files" in error for error in errors))
        self.assertTrue(any("runtime_seconds" in error for error in errors))
        self.assertTrue(any("token_source" in error for error in errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(inconsistent))

    def test_live_single_rejects_producer_state_tampering_before_controller_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root, tamper_state=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        details = report["scenarios"][0]["details"]
        self.assertFalse(details["producer_scope_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertEqual(details["integrated_files"], [])
        self.assertEqual(details["controller_verify_status"], "not-run")
        self.assertIn(".ai-team/state/harness.db", details["producer_changed_files"])

    def test_live_parallel_rejects_cross_producer_file_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root, tamper_attribution=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex-parallel",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        details = report["scenarios"][0]["details"]
        self.assertFalse(details["producer_attribution_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertEqual(details["integrated_files"], [])
        alpha = next(item for item in details["producers"] if item["task"] == "LIVE-P1")
        self.assertEqual(alpha["changed_files"], ["alpha.py", "beta.py"])

    def test_should_fail_thresholds(self) -> None:
        scenario_count = len(run_agent_e2e_eval.FIXTURE_SCENARIOS) + len(
            run_agent_e2e_eval.STABILITY_SCENARIOS
        )
        scenarios = [
            {
                "name": f"scenario-{index}",
                "pass": True,
                "skip_reason": "",
                "duration_seconds": 0.0,
                "details": (
                    {
                        "forged_evidence_block_count": 1,
                        "expected_human_review_required_count": 1,
                    }
                    if index == 0
                    else {}
                ),
            }
            for index in range(scenario_count)
        ]
        base = run_agent_e2e_eval.summarize("stability", scenarios, time.perf_counter())
        self.assertEqual(run_agent_e2e_eval.report_consistency_errors(base), [])
        self.assertFalse(run_agent_e2e_eval.should_fail(base))
        locked = json.loads(json.dumps(base))
        locked["summary"]["sqlite_lock_error_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(locked))
        false_pass = json.loads(json.dumps(base))
        false_pass["summary"]["false_pass_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(false_pass))
        human_intervention = json.loads(json.dumps(base))
        human_intervention["summary"]["human_intervention_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(human_intervention))
        skipped = json.loads(json.dumps(base))
        skipped["summary"]["skipped_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(skipped))
        missing_forged_block = json.loads(json.dumps(base))
        missing_forged_block["summary"]["forged_evidence_block_count"] = 0
        self.assertTrue(run_agent_e2e_eval.should_fail(missing_forged_block))
        live_skipped = {"mode": "live-codex", "live_skipped": True, "summary": {"failed_count": 0}}
        self.assertTrue(run_agent_e2e_eval.should_fail(live_skipped))

    def test_out_matches_stdout_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            evidence_out = Path(temp) / "evidence.json"
            result = run_eval_process(
                "--mode",
                "live-codex",
                "--out",
                str(out),
                "--evidence-out",
                str(evidence_out),
                env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""},
            )
            report = json.loads(result.stdout)
            from_file = json.loads(out.read_text(encoding="utf-8"))
            evidence = json.loads(evidence_out.read_text(encoding="utf-8"))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(from_file, report)
        self.assertEqual(evidence, run_agent_e2e_eval.compact_evidence_report(report))
        self.assertIn("matrix", from_file)
        self.assertIn("summary", from_file)
        self.assertIn("scenarios", from_file)


if __name__ == "__main__":
    unittest.main()

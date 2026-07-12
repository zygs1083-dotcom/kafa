from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core.cycle_ledger import current_candidate_sha  # noqa: E402
from core.projections import PROJECTION_ROLLBACK_PATHS  # noqa: E402
from core.schema_lifecycle import create_schema30  # noqa: E402
import harness_db  # noqa: E402
import harness_lib  # noqa: E402


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def prepare_cli_validation(root: Path) -> None:
    harness_db.render_all(root)


def prepare_cli_candidate_config(root: Path) -> None:
    (root / ".gitignore").write_text(
        "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
        encoding="utf-8",
    )


def delivery_module():
    spec = importlib.util.find_spec("core.delivery")
    if spec is None:
        raise AssertionError("core.delivery local policy module is missing")
    return importlib.import_module("core.delivery")


def create_schema30_delivery_fixture(
    root: Path,
    *,
    sandbox_status: str = "available",
    no_network: int = 1,
    failure_mode_status: str | None = None,
) -> Path:
    (root / "candidate.py").write_bytes(b"VALUE = 1\n")
    artifact = root / ".ai-team/runtime/execution.out"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        '{"summary": {"total": 1, "passed": 1, "failed": 0, "errors": 0}}\n',
        encoding="utf-8",
    )
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    candidate = current_candidate_sha(root)
    db = root / ".ai-team/state/harness.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("begin immediate")
        create_schema30(conn)
        conn.execute(
            """
            insert into project
            (id, project_id, schema_version, runtime_version, phase, current_cycle_id,
             status, scope_status, current_owner, revision, updated_at)
            values (1, 'test-project', 30, '5.0.0', 'delivery_readiness', 'CYCLE-current',
                    'active', 'confirmed', 'controller', 1, '2026-07-11T00:00:00Z')
            """
        )
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at,
             closed_at, created_at, updated_at)
            values ('CYCLE-current', 'Current', 'Deliver safely', 'active',
                    'delivery_readiness', '', ?, '2026-07-11T00:00:00Z', '',
                    '2026-07-11T00:00:00Z', '2026-07-11T00:00:00Z')
            """,
            (candidate,),
        )
        conn.execute(
            """
            insert into test_targets
            (id, kind, command_template, description, gateable, requires_sandbox,
             requires_no_network, result_format, result_path, created_at, updated_at)
            values ('UNIT', 'unit', 'python3 -m unittest', 'structured unit target', 1,
                    1, 1, 'pytest-json', '.ai-team/runtime/execution.out',
                    '2026-07-11T00:00:00Z', '2026-07-11T00:00:00Z')
            """
        )
        conn.execute(
            """
            insert into executions
            (id, cycle_id, candidate_sha, target_id, command, exit_code, stdout_sha256,
             artifact_path, executed_count, result_format, semantic_status, runner,
             sandbox_status, no_network, policy_status, created_at)
            values ('EX1', 'CYCLE-current', ?, 'UNIT', 'python3 -m unittest', 0, ?,
                    '.ai-team/runtime/execution.out', 1, 'pytest-json', 'pass', 'local', ?, ?,
                    'allowed', '2026-07-11T00:00:00Z')
            """,
            (candidate, artifact_sha, sandbox_status, no_network),
        )
        conn.execute(
            """
            insert into validations
            (id, cycle_id, candidate_sha, acceptance_id, surface, result,
             validation_status, findings, residual_risk, created_at)
            values ('V1', 'CYCLE-current', ?, null, 'unit', 'pass', 'active', '', '',
                    '2026-07-11T00:00:00Z')
            """,
            (candidate,),
        )
        conn.execute(
            """insert into validation_executions
            (validation_id, execution_id, cycle_id, candidate_sha)
            values ('V1', 'EX1', 'CYCLE-current', ?)""",
            (candidate,),
        )
        conn.execute(
            """
            insert into quality_gates
            (id, sequence, cycle_id, candidate_sha, gate_status, gate,
             producer_context_id, reviewer_context_id, review_status, result,
             blocking_findings, residual_risk, reviewed_revision, created_at)
            values ('G1', 1, 'CYCLE-current', ?, 'active', 'independent_qa',
                    'producer-context', 'reviewer-context', 'reviewed-local', 'pass',
                    '', '', 1, '2026-07-11T00:00:00Z')
            """,
            (candidate,),
        )
        if failure_mode_status is not None:
            accepted = failure_mode_status in {"accepted", "exempt"}
            conn.execute(
                """
                insert into failure_modes
                (id, cycle_id, feature, scenario, trigger, expected_behavior, risk,
                 status, accepted_by, acceptance_reason, acceptance_scope,
                 accepted_revision, expires_at, revision)
                values ('FM1', 'CYCLE-current', 'delivery', 'high-risk path', 'delivery',
                        'fail closed', 'high', ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    failure_mode_status,
                    "user" if accepted else "",
                    "candidate-specific acceptance" if accepted else "",
                    "candidate" if accepted else "",
                    1 if accepted else None,
                    "2099-01-01T00:00:00Z" if accepted else "",
                ),
            )
            conn.execute(
                """
                insert into validation_failure_modes
                (validation_id, cycle_id, failure_mode_id)
                values ('V1', 'CYCLE-current', 'FM1')
                """
            )
        conn.commit()
    return db


def unlink_git_object(path: Path) -> None:
    """Remove a loose Git object even when Windows marks it read-only."""

    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, path.stat().st_mode | stat.S_IWUSR)
        path.unlink()


def schema30_issues(root: Path) -> list[str]:
    delivery = delivery_module()
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return delivery.evaluate_schema30_delivery_readiness(
            conn,
            root,
            is_expired=lambda value: value <= "2026-07-11T00:00:00Z",
            observed_at="2026-07-11T00:00:00Z",
        )


class HonestHighRiskPolicyTests(unittest.TestCase):
    def test_degraded_review_cannot_use_high_risk_accepted_path_with_distinct_ids(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"high"},
            structured_current_execution=True,
            producer_context_id="producer-context",
            reviewer_context_id="different-looking-reviewer-context",
            review_status="same-context-degraded",
            risk_acceptances=[
                {
                    "risk_id": "FM1",
                    "risk": "high",
                    "status": "accepted",
                    "actor": "user",
                    "reason": "candidate-specific acceptance",
                    "scope": "candidate",
                    "revision": 7,
                    "expires_at": "2026-07-12T00:00:00Z",
                }
            ],
            required_risk_ids={"FM1"},
            current_revision=7,
            now="2026-07-11T00:00:00Z",
        )

        self.assertEqual(decision.status, "human-review-required")
        self.assertFalse(decision.delivery_allowed)
        self.assertIn("reviewed-local", " ".join(decision.reasons))

    def test_accepted_high_finding_still_requires_reviewed_local_gate(self) -> None:
        delivery = delivery_module()
        with tempfile.TemporaryDirectory() as temp:
            degraded_root = Path(temp) / "degraded"
            degraded_root.mkdir()
            degraded_db = create_schema30_delivery_fixture(degraded_root)
            reviewed_root = Path(temp) / "reviewed"
            reviewed_root.mkdir()
            reviewed_db = create_schema30_delivery_fixture(reviewed_root)

            for db, review_status in (
                (degraded_db, "same-context-degraded"),
                (reviewed_db, "reviewed-local"),
            ):
                with closing(sqlite3.connect(db)) as conn:
                    conn.row_factory = sqlite3.Row
                    candidate = conn.execute(
                        "select candidate_sha from delivery_cycles where id='CYCLE-current'"
                    ).fetchone()[0]
                    conn.execute(
                        "update quality_gates set review_status = ? where id = 'G1'",
                        (review_status,),
                    )
                    conn.execute(
                        """
                        insert into findings
                        (id, cycle_id, candidate_sha, surface, severity, status, summary,
                         waived_by, waiver_reason, waiver_scope, waived_revision,
                         waiver_expires_at, created_at)
                        values ('F-accepted-high', 'CYCLE-current', ?, 'delivery', 'high',
                                'accepted', 'accepted high finding remains high risk',
                                'user', 'candidate-specific acceptance', 'candidate', 1,
                                '2099-01-01T00:00:00Z', '2026-07-11T00:00:00Z')
                        """,
                        (candidate,),
                    )
                    conn.commit()

            with closing(sqlite3.connect(degraded_db)) as conn:
                conn.row_factory = sqlite3.Row
                degraded_issues, degraded_decision = delivery.evaluate_schema30_delivery(
                    conn,
                    degraded_root,
                    is_expired=lambda _: False,
                    observed_at="2026-07-11T00:00:00Z",
                )

            self.assertEqual(degraded_decision.status, "human-review-required")
            self.assertFalse(degraded_decision.delivery_allowed)
            self.assertIn("reviewed-local", " ".join(degraded_issues))
            with self.assertRaisesRegex(harness_db.HarnessError, "delivery record blocked"):
                harness_db.record_delivery(degraded_root, "local")

            harness_db.record_delivery(reviewed_root, "local")
            with closing(sqlite3.connect(reviewed_db)) as conn:
                reviewed_status = conn.execute(
                    "select decision_status from deliveries"
                ).fetchone()[0]
            self.assertEqual(reviewed_status, "accepted-risk")

    def test_local_trust_states_are_explicit_and_non_cryptographic(self) -> None:
        delivery = delivery_module()
        cases = (
            ("", "", "controller-verified"),
            ("producer", "reviewer", "reviewed-local"),
            ("same", "same", "same-context-degraded"),
        )
        for producer, reviewer, expected in cases:
            with self.subTest(expected=expected):
                decision = delivery.evaluate_local_trust(
                    risk_levels={"medium"},
                    structured_current_execution=True,
                    producer_context_id=producer,
                    reviewer_context_id=reviewer,
                    review_status=expected,
                    risk_acceptances=[],
                    now="2026-07-11T00:00:00Z",
                )
                self.assertEqual(decision.status, expected)
                self.assertEqual(decision.trust_level, expected)
                self.assertTrue(decision.delivery_allowed)

    def test_low_medium_explicit_degraded_review_keeps_degraded_label(self) -> None:
        delivery = delivery_module()
        for risk in ("low", "medium"):
            with self.subTest(risk=risk):
                decision = delivery.evaluate_local_trust(
                    risk_levels={risk},
                    structured_current_execution=True,
                    producer_context_id="producer-context",
                    reviewer_context_id="different-looking-reviewer-context",
                    review_status="same-context-degraded",
                    risk_acceptances=[],
                    now="2026-07-11T00:00:00Z",
                )

                self.assertEqual(decision.status, "same-context-degraded")
                self.assertEqual(decision.trust_level, "same-context-degraded")
                self.assertTrue(decision.delivery_allowed)

    def test_noncanonical_review_statuses_fail_closed(self) -> None:
        delivery = delivery_module()
        cases = (
            ("high", " reviewed-local "),
            ("medium", "unknown"),
            ("low", "REVIEWED-LOCAL"),
        )
        for risk, review_status in cases:
            with self.subTest(risk=risk, review_status=review_status):
                acceptance = {
                    "risk_id": "FM1",
                    "risk": risk,
                    "status": "accepted",
                    "actor": "user",
                    "reason": "candidate-specific acceptance",
                    "scope": "candidate",
                    "revision": 7,
                    "expires_at": "2026-07-12T00:00:00Z",
                }
                decision = delivery.evaluate_local_trust(
                    risk_levels={risk},
                    structured_current_execution=True,
                    producer_context_id="producer-context",
                    reviewer_context_id="reviewer-context",
                    review_status=review_status,
                    risk_acceptances=[acceptance] if risk == "high" else [],
                    required_risk_ids={"FM1"} if risk == "high" else set(),
                    current_revision=7,
                    now="2026-07-11T00:00:00Z",
                )

                self.assertEqual(decision.status, "human-review-required")
                self.assertFalse(decision.delivery_allowed)

    def test_high_risk_without_acceptance_returns_human_review_required(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"high"},
            structured_current_execution=True,
            producer_context_id="producer-context",
            reviewer_context_id="reviewer-context",
            review_status="reviewed-local",
            risk_acceptances=[],
            now="2026-07-11T00:00:00Z",
        )
        self.assertEqual(decision.status, "human-review-required")
        self.assertFalse(decision.delivery_allowed)

    def test_same_context_high_risk_gate_is_rejected(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"critical"},
            structured_current_execution=True,
            producer_context_id="same-context",
            reviewer_context_id="same-context",
            review_status="same-context-degraded",
            risk_acceptances=[],
            now="2026-07-11T00:00:00Z",
        )
        self.assertEqual(decision.status, "human-review-required")
        self.assertIn("distinct reviewer", " ".join(decision.reasons).lower())
        self.assertFalse(decision.delivery_allowed)

    def test_complete_unexpired_acceptance_uses_procedural_path(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"high"},
            structured_current_execution=True,
            producer_context_id="producer-context",
            reviewer_context_id="reviewer-context",
            review_status="reviewed-local",
            risk_acceptances=[
                {
                    "risk": "high",
                    "status": "accepted",
                    "actor": "user",
                    "reason": "explicitly accepted for this candidate",
                    "scope": "candidate",
                    "revision": 7,
                    "expires_at": "2026-07-12T00:00:00Z",
                }
            ],
            current_revision=7,
            now="2026-07-11T00:00:00Z",
        )
        self.assertEqual(decision.status, "accepted-risk")
        self.assertEqual(decision.trust_level, "procedural")
        self.assertTrue(decision.delivery_allowed)

    def test_high_risk_acceptance_requires_explicit_status_and_current_revision(self) -> None:
        delivery = delivery_module()
        acceptance = {
            "risk_id": "FM1",
            "risk": "high",
            "status": "accepted",
            "actor": "user",
            "reason": "candidate-specific acceptance",
            "scope": "candidate",
            "revision": 7,
            "expires_at": "2026-07-12T00:00:00Z",
        }
        cases = (
            ({key: value for key, value in acceptance.items() if key != "status"}, 7, "status"),
            (acceptance, None, "current project revision"),
        )
        for record, current_revision, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                decision = delivery.evaluate_local_trust(
                    risk_levels={"high"},
                    structured_current_execution=True,
                    producer_context_id="producer-context",
                    reviewer_context_id="reviewer-context",
                    review_status="reviewed-local",
                    risk_acceptances=[record],
                    required_risk_ids={"FM1"},
                    current_revision=current_revision,
                    now="2026-07-11T00:00:00Z",
                )

                self.assertEqual(decision.status, "human-review-required")
                self.assertFalse(decision.delivery_allowed)
                self.assertIn(expected_reason, " ".join(decision.reasons))

    def test_every_high_risk_id_must_have_complete_accepted_or_exempt_record(self) -> None:
        delivery = delivery_module()
        records = [
            {
                "risk_id": "FM-high",
                "risk": "high",
                "status": "accepted",
                "actor": "user",
                "reason": "candidate-specific acceptance",
                "scope": "candidate",
                "revision": 7,
                "expires_at": "2026-07-12T00:00:00Z",
            },
            {
                "risk_id": "FM-critical",
                "risk": "critical",
                "status": "exempt",
                "actor": "user",
                "reason": "not applicable to this candidate",
                "scope": "candidate",
                "revision": 7,
                "expires_at": "2026-07-12T00:00:00Z",
            },
        ]
        decision = delivery.evaluate_local_trust(
            risk_levels={"high", "critical"},
            structured_current_execution=True,
            producer_context_id="producer-context",
            reviewer_context_id="reviewer-context",
            review_status="reviewed-local",
            risk_acceptances=records,
            required_risk_ids={"FM-high", "FM-critical"},
            current_revision=7,
            now="2026-07-11T00:00:00Z",
        )

        self.assertEqual(decision.status, "accepted-risk")
        self.assertEqual(decision.trust_level, "procedural")
        self.assertTrue(decision.delivery_allowed)

    def test_forged_context_hmac_and_stale_ci_looking_fields_never_upgrade_trust(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"critical"},
            structured_current_execution=True,
            producer_context_id="forged-producer-session",
            reviewer_context_id="forged-reviewer-session",
            review_status="reviewed-local",
            risk_acceptances=[
                {
                    "risk": "critical",
                    "verification_token": "hmac:" + "a" * 64,
                    "ci_conclusion": "success",
                    "ci_candidate": "stale-candidate",
                }
            ],
            now="2026-07-11T00:00:00Z",
        )

        self.assertEqual(decision.status, "human-review-required")
        self.assertFalse(decision.delivery_allowed)
        self.assertIn("incomplete", " ".join(decision.reasons))

    def test_expired_or_stale_accepted_risk_fails_closed(self) -> None:
        delivery = delivery_module()
        cases = (
            (7, "2026-07-11T00:00:00Z", "expired"),
            (6, "2026-07-12T00:00:00Z", "stale"),
        )
        for revision, expires_at, expected in cases:
            with self.subTest(expected=expected):
                decision = delivery.evaluate_local_trust(
                    risk_levels={"high"},
                    structured_current_execution=True,
                    producer_context_id="producer",
                    reviewer_context_id="reviewer",
                    review_status="reviewed-local",
                    risk_acceptances=[
                        {
                            "risk_id": "FM1",
                            "risk": "high",
                            "status": "accepted",
                            "actor": "user",
                            "reason": "temporary acceptance",
                            "scope": "candidate",
                            "revision": revision,
                            "expires_at": expires_at,
                        }
                    ],
                    required_risk_ids={"FM1"},
                    current_revision=7,
                    now="2026-07-11T00:00:00Z",
                )
                self.assertEqual(decision.status, "human-review-required")
                self.assertFalse(decision.delivery_allowed)
                self.assertIn(expected, " ".join(decision.reasons))

    def test_one_acceptance_cannot_cover_two_same_level_risk_ids(self) -> None:
        delivery = delivery_module()
        decision = delivery.evaluate_local_trust(
            risk_levels={"high"},
            structured_current_execution=True,
            producer_context_id="producer",
            reviewer_context_id="reviewer",
            review_status="reviewed-local",
            risk_acceptances=[
                {
                    "risk_id": "FM1",
                    "risk": "high",
                    "status": "accepted",
                    "actor": "user",
                    "reason": "only one risk accepted",
                    "scope": "candidate",
                    "revision": 7,
                    "expires_at": "2026-07-12T00:00:00Z",
                }
            ],
            required_risk_ids={"FM1", "FM2"},
            current_revision=7,
            now="2026-07-11T00:00:00Z",
        )

        self.assertEqual(decision.status, "human-review-required")
        self.assertIn("FM2", " ".join(decision.reasons))
        self.assertFalse(decision.delivery_allowed)


class Schema30DeliveryDecisionTests(unittest.TestCase):
    def test_candidate_identity_excludes_only_exact_generated_non_state_paths(self) -> None:
        expected = {
            path.as_posix()
            for path in PROJECTION_ROLLBACK_PATHS
            if path.as_posix().startswith("docs/harness/")
        } | {
            ".codex/agents/architect.toml",
            ".codex/agents/developer.toml",
            ".codex/agents/qa-reviewer.toml",
        }
        self.assertEqual(harness_lib.HARNESS_EXACT_SOURCE_PATHS, expected)

    def test_schema30_degraded_gate_cannot_pass_high_risk_with_accepted_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_delivery_fixture(root, failure_mode_status="accepted")
            with closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    """
                    update quality_gates
                    set review_status='same-context-degraded',
                        producer_context_id='producer-context',
                        reviewer_context_id='different-looking-reviewer-context'
                    where id='G1'
                    """
                )
                conn.commit()

            issues = schema30_issues(root)

        self.assertIn("human-review-required", " ".join(issues))
        self.assertIn("reviewed-local", " ".join(issues))

    def test_cli_delivery_validation_rejects_degraded_spoof_and_accepts_reviewed_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            degraded_root = Path(temp) / "degraded"
            degraded_root.mkdir()
            prepare_cli_candidate_config(degraded_root)
            degraded_db = create_schema30_delivery_fixture(
                degraded_root,
                failure_mode_status="accepted",
            )
            with closing(sqlite3.connect(degraded_db)) as conn:
                conn.execute(
                    """
                    update quality_gates
                    set review_status='same-context-degraded',
                        producer_context_id='producer-context',
                        reviewer_context_id='different-looking-reviewer-context'
                    where id='G1'
                    """
                )
                conn.commit()
            prepare_cli_validation(degraded_root)
            degraded = run_harness(degraded_root, "validate", "--delivery")

            reviewed_root = Path(temp) / "reviewed"
            reviewed_root.mkdir()
            prepare_cli_candidate_config(reviewed_root)
            create_schema30_delivery_fixture(
                reviewed_root,
                failure_mode_status="accepted",
            )
            prepare_cli_validation(reviewed_root)
            reviewed = run_harness(reviewed_root, "validate", "--delivery")

        self.assertNotEqual(degraded.returncode, 0)
        self.assertIn("human-review-required", degraded.stdout)
        self.assertIn("review_status=reviewed-local", degraded.stdout)
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        self.assertIn("OK: harness state is valid", reviewed.stdout)

    def test_schema30_low_risk_and_accepted_risk_paths_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            low_root = Path(temp) / "low"
            low_root.mkdir()
            create_schema30_delivery_fixture(low_root)
            low_issues = schema30_issues(low_root)

            accepted_root = Path(temp) / "accepted"
            accepted_root.mkdir()
            create_schema30_delivery_fixture(
                accepted_root,
                failure_mode_status="accepted",
            )
            accepted_issues = schema30_issues(accepted_root)

        self.assertEqual(low_issues, [])
        self.assertEqual(accepted_issues, [])

    def test_delivery_persists_degraded_and_accepted_risk_decision_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            degraded_root = Path(temp) / "degraded"
            degraded_root.mkdir()
            degraded_db = create_schema30_delivery_fixture(degraded_root)
            with closing(sqlite3.connect(degraded_db)) as conn:
                conn.execute(
                    """
                    insert into failure_modes
                    (id, cycle_id, feature, scenario, trigger, expected_behavior, risk,
                     status, revision)
                    values ('FM-medium', 'CYCLE-current', 'delivery', 'degraded review',
                            'delivery', 'retain degraded label', 'medium', 'identified', 1)
                    """
                )
                conn.execute(
                    """
                    update quality_gates
                    set review_status='same-context-degraded',
                        reviewer_context_id=producer_context_id
                    where id='G1'
                    """
                )
                conn.commit()
            harness_db.record_delivery(degraded_root, "local")
            with closing(sqlite3.connect(degraded_db)) as conn:
                degraded_status = conn.execute(
                    "select decision_status from deliveries"
                ).fetchone()[0]

            accepted_root = Path(temp) / "accepted"
            accepted_root.mkdir()
            accepted_db = create_schema30_delivery_fixture(
                accepted_root,
                failure_mode_status="accepted",
            )
            harness_db.record_delivery(accepted_root, "local")
            with closing(sqlite3.connect(accepted_db)) as conn:
                accepted_status = conn.execute(
                    "select decision_status from deliveries"
                ).fetchone()[0]

            degraded_projection = (
                degraded_root / "docs/harness/delivery.md"
            ).read_text(encoding="utf-8")
            accepted_projection = (
                accepted_root / "docs/harness/delivery.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(degraded_status, "same-context-degraded")
        self.assertEqual(accepted_status, "accepted-risk")
        self.assertIn("Decision Status\nsame-context-degraded", degraded_projection)
        self.assertIn("Decision Status\naccepted-risk", accepted_projection)

    def test_fractional_revision_metadata_blocks_delivery(self) -> None:
        cases = ("accepted-risk", "quality-gate", "project", "finding")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_delivery_fixture(
                    root,
                    failure_mode_status="accepted" if case != "finding" else None,
                )
                with closing(sqlite3.connect(db)) as conn:
                    if case == "accepted-risk":
                        conn.execute(
                            "update failure_modes set accepted_revision=1.9 where id='FM1'"
                        )
                    elif case == "quality-gate":
                        conn.execute(
                            "update quality_gates set reviewed_revision=1.9 where id='G1'"
                        )
                    elif case == "project":
                        conn.execute("update project set revision=1.9 where id=1")
                    else:
                        candidate = conn.execute(
                            "select candidate_sha from delivery_cycles where id='CYCLE-current'"
                        ).fetchone()[0]
                        conn.execute(
                            """
                            insert into findings
                            (id, cycle_id, candidate_sha, surface, severity, status, summary,
                             waived_by, waiver_reason, waiver_scope, waived_revision,
                             waiver_expires_at, created_at)
                            values ('F-real-revision', 'CYCLE-current', ?, 'delivery', 'high',
                                    'accepted', 'fractional revision must not be truncated',
                                    'user', 'candidate-specific acceptance', 'candidate', 1.9,
                                    '2099-01-01T00:00:00Z', '2026-07-11T00:00:00Z')
                            """,
                            (candidate,),
                        )
                    conn.commit()

                issues = schema30_issues(root)
                self.assertTrue(issues, f"fractional {case} revision was accepted")
                with self.assertRaisesRegex(harness_db.HarnessError, "delivery record blocked"):
                    harness_db.record_delivery(root, "local")

    def test_fractional_execution_metadata_blocks_delivery(self) -> None:
        cases = (
            ("test_targets", "gateable", 1.9),
            ("test_targets", "requires_sandbox", 1.9),
            ("test_targets", "requires_no_network", 1.9),
            ("executions", "exit_code", 0.9),
            ("executions", "executed_count", 1.9),
            ("executions", "no_network", 1.9),
        )
        for table, field, value in cases:
            with self.subTest(table=table, field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_delivery_fixture(root)
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute("pragma ignore_check_constraints = on")
                    if table == "executions":
                        conn.execute("drop trigger executions_no_update")
                    row_id = "UNIT" if table == "test_targets" else "EX1"
                    conn.execute(
                        f"update {table} set {field} = ? where id = ?",
                        (value, row_id),
                    )
                    conn.commit()

                issues = schema30_issues(root)
                self.assertTrue(
                    issues,
                    f"fractional {table}.{field} passed delivery evaluation",
                )
                with self.assertRaisesRegex(harness_db.HarnessError, "delivery record blocked"):
                    harness_db.record_delivery(root, "local")

    def test_invalid_finding_expiry_is_rejected_on_write_and_blocks_tampered_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_delivery_fixture(root)
            invalid = run_harness(
                root,
                "finding",
                "record",
                "--id",
                "F-invalid-expiry",
                "--surface",
                "delivery",
                "--severity",
                "critical",
                "--status",
                "accepted",
                "--summary",
                "malformed expiry must fail closed",
                "--waived-by",
                "user",
                "--waiver-reason",
                "candidate-specific acceptance",
                "--waiver-scope",
                "candidate",
                "--waived-revision",
                "1",
                "--waiver-expires-at",
                "not-a-timestamp",
            )
            with closing(sqlite3.connect(db)) as conn:
                candidate = conn.execute(
                    "select candidate_sha from delivery_cycles where id='CYCLE-current'"
                ).fetchone()[0]
                recorded_count = int(
                    conn.execute(
                        "select count(*) from findings where id='F-invalid-expiry'"
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    insert into findings
                    (id, cycle_id, candidate_sha, surface, severity, status, summary,
                     waived_by, waiver_reason, waiver_scope, waived_revision,
                     waiver_expires_at, created_at)
                    values ('F-tampered-expiry', 'CYCLE-current', ?, 'delivery',
                            'critical', 'accepted', 'tampered malformed expiry',
                            'user', 'candidate-specific acceptance', 'candidate', 1,
                            'not-a-timestamp', '2026-07-11T00:00:00Z')
                    """,
                    (candidate,),
                )
                conn.commit()

            issues = schema30_issues(root)

        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("expiry", (invalid.stdout + invalid.stderr).lower())
        self.assertEqual(recorded_count, 0)
        self.assertIn("F-tampered-expiry", " ".join(issues))

    def test_whitespace_finding_waiver_is_rejected_and_tampered_data_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            write_root = Path(temp) / "write"
            write_root.mkdir()
            write_db = create_schema30_delivery_fixture(write_root)
            invalid = run_harness(
                write_root,
                "finding",
                "record",
                "--id",
                "F-whitespace-write",
                "--surface",
                "delivery",
                "--severity",
                "critical",
                "--status",
                "accepted",
                "--summary",
                "whitespace metadata must not count",
                "--waived-by",
                " ",
                "--waiver-reason",
                " ",
                "--waiver-scope",
                " ",
                "--waived-revision",
                "1",
                "--waiver-expires-at",
                "2099-01-01T00:00:00Z",
            )
            with closing(sqlite3.connect(write_db)) as conn:
                recorded_count = int(
                    conn.execute(
                        "select count(*) from findings where id='F-whitespace-write'"
                    ).fetchone()[0]
                )

            tampered_root = Path(temp) / "tampered"
            tampered_root.mkdir()
            tampered_db = create_schema30_delivery_fixture(tampered_root)
            with closing(sqlite3.connect(tampered_db)) as conn:
                candidate, revision = conn.execute(
                    """
                    select dc.candidate_sha, p.revision
                    from delivery_cycles dc cross join project p
                    where dc.id='CYCLE-current' and p.id=1
                    """
                ).fetchone()
                conn.execute(
                    """
                    insert into findings
                    (id, cycle_id, candidate_sha, surface, severity, status, summary,
                     waived_by, waiver_reason, waiver_scope, waived_revision,
                     waiver_expires_at, created_at)
                    values ('F-whitespace-tampered', 'CYCLE-current', ?, 'delivery',
                            'critical', 'accepted', 'tampered whitespace waiver',
                            ' ', ' ', ' ', ?, '2099-01-01T00:00:00Z',
                            '2026-07-11T00:00:00Z')
                    """,
                    (candidate, revision),
                )
                conn.commit()

            issues = schema30_issues(tampered_root)

        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("requires actor", (invalid.stdout + invalid.stderr).lower())
        self.assertEqual(recorded_count, 0)
        self.assertIn("F-whitespace-tampered", " ".join(issues))

    def test_schema30_current_candidate_latest_gate_finding_and_invalidation_fail_closed(self) -> None:
        cases = ("candidate", "gate", "finding", "invalidation")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_delivery_fixture(root)
                if case == "candidate":
                    (root / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
                else:
                    with closing(sqlite3.connect(db)) as conn:
                        if case == "gate":
                            conn.execute(
                                "update quality_gates set result='fail' where id='G1'"
                            )
                        elif case == "finding":
                            candidate = conn.execute(
                                "select candidate_sha from delivery_cycles where id='CYCLE-current'"
                            ).fetchone()[0]
                            conn.execute(
                                """
                                insert into findings
                                (id, cycle_id, candidate_sha, surface, severity, status,
                                 summary, created_at)
                                values ('F1', 'CYCLE-current', ?, 'delivery', 'critical',
                                        'open', 'blocker', '2026-07-11T00:00:00Z')
                                """,
                                (candidate,),
                            )
                        else:
                            conn.execute(
                                """
                                insert into invalidations
                                (id, cycle_id, source_type, source_id, target_type,
                                 target_id, reason, created_at)
                                values ('I1', 'CYCLE-current', 'validation', 'V1',
                                        'quality_gate', 'G1', 'candidate changed',
                                        '2026-07-11T00:00:00Z')
                                """
                            )
                        conn.commit()
                issues = schema30_issues(root)

            combined = " ".join(issues)
            expected = {
                "candidate": "current candidate",
                "gate": "latest quality gate is not pass",
                "finding": "critical finding blocks delivery",
                "invalidation": "stale runtime artifact",
            }[case]
            self.assertIn(expected, combined)

    def test_schema30_sandbox_and_no_network_policy_fail_closed(self) -> None:
        cases = (("unavailable", 1, "available sandbox"), ("available", 0, "no-network"))
        for sandbox_status, no_network, expected in cases:
            with self.subTest(sandbox_status=sandbox_status, no_network=no_network), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                create_schema30_delivery_fixture(
                    root,
                    sandbox_status=sandbox_status,
                    no_network=no_network,
                )
                issues = schema30_issues(root)
            self.assertIn(expected, " ".join(issues))

    def test_schema30_dirty_git_worktree_after_gate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "candidate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            create_schema30_delivery_fixture(root)
            reviewed_candidate = current_candidate_sha(root)
            (root / "dirty.txt").write_text("not reviewed\n", encoding="utf-8")

            changed_candidate = current_candidate_sha(root)
            issues = schema30_issues(root)

        self.assertNotEqual(reviewed_candidate, changed_candidate)
        self.assertIn("current candidate", " ".join(issues))

    def test_ignored_runtime_source_change_invalidates_schema30_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_bytes(
                (
                    "\n".join(
                        [
                            *harness_db.RUNTIME_GITIGNORE_PATTERNS,
                            "runtime_extension.py",
                        ]
                    )
                    + "\n"
                ).encode("utf-8")
            )
            (root / "candidate.py").write_bytes(b"VALUE = 1\n")
            (root / "loader.py").write_bytes(b"import runtime_extension\n")
            extension = root / "runtime_extension.py"
            extension.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore", "candidate.py", "loader.py"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "candidate with ignored runtime module"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            create_schema30_delivery_fixture(root)
            reviewed_candidate = current_candidate_sha(root)
            self.assertEqual(schema30_issues(root), [])

            extension.write_text(
                "raise RuntimeError('ignored runtime drift')\n",
                encoding="utf-8",
            )
            changed_candidate = current_candidate_sha(root)
            issues = schema30_issues(root)

        self.assertNotEqual(reviewed_candidate, changed_candidate)
        self.assertIn("current candidate", " ".join(issues))

    def test_candidate_identity_excludes_top_level_dependency_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text(
                ".venv/\n.ruff_cache/\nruntime_extension.py\n",
                encoding="utf-8",
            )
            (root / "loader.py").write_text(
                "import runtime_extension\n",
                encoding="utf-8",
            )
            extension = root / "runtime_extension.py"
            extension.write_text("VALUE = 1\n", encoding="utf-8")
            lockfile = root / "requirements.lock"
            lockfile.write_text("example==1\n", encoding="utf-8")
            environment_python = root / ".venv/bin/python"
            environment_python.parent.mkdir(parents=True)
            environment_python.write_bytes(b"generated environment executable")
            tool_cache = root / ".ruff_cache/state"
            tool_cache.parent.mkdir(parents=True)
            tool_cache.write_text("cache v1\n", encoding="utf-8")
            adjacent_python = root / ".venvish/bin/python"
            adjacent_python.parent.mkdir(parents=True)
            adjacent_python.write_bytes(b"ordinary ignored source")
            subprocess.run(
                ["git", "add", ".gitignore", "loader.py", "requirements.lock"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "candidate with local dependency environment"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            original_is_symlink = Path.is_symlink

            def report_environment_symlink(path: Path) -> bool:
                if path.resolve() == environment_python.resolve():
                    return True
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", new=report_environment_symlink):
                original = current_candidate_sha(root)
                tool_cache.write_text("cache v2\n", encoding="utf-8")
                cache_changed = current_candidate_sha(root)
                extension.write_text("VALUE = 2\n", encoding="utf-8")
                ignored_source_changed = current_candidate_sha(root)
                lockfile.write_text("example==2\n", encoding="utf-8")
                lockfile_changed = current_candidate_sha(root)

            self.assertEqual(original, cache_changed)
            self.assertNotEqual(original, ignored_source_changed)
            self.assertNotEqual(ignored_source_changed, lockfile_changed)

            def report_adjacent_symlink(path: Path) -> bool:
                if path.resolve() == adjacent_python.resolve():
                    return True
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", new=report_adjacent_symlink):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    current_candidate_sha(root)

    def test_content_candidate_identity_excludes_top_level_dependency_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            environment_python = root / ".venv/bin/python"
            environment_python.parent.mkdir(parents=True)
            environment_python.write_bytes(b"generated environment executable")
            tool_cache = root / ".ruff_cache/state"
            tool_cache.parent.mkdir(parents=True)
            tool_cache.write_text("cache v1\n", encoding="utf-8")
            adjacent_python = root / ".venvish/bin/python"
            adjacent_python.parent.mkdir(parents=True)
            adjacent_python.write_bytes(b"ordinary source")
            original_is_symlink = Path.is_symlink

            def report_environment_symlink(path: Path) -> bool:
                if path.resolve() == environment_python.resolve():
                    return True
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", new=report_environment_symlink):
                original = current_candidate_sha(root)
                tool_cache.write_text("cache v2\n", encoding="utf-8")
                cache_changed = current_candidate_sha(root)
                source.write_text("VALUE = 2\n", encoding="utf-8")
                changed = current_candidate_sha(root)
            self.assertEqual(original, cache_changed)
            self.assertNotEqual(original, changed)

            def report_adjacent_symlink(path: Path) -> bool:
                if path.resolve() == adjacent_python.resolve():
                    return True
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", new=report_adjacent_symlink):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    current_candidate_sha(root)

    def test_versioned_dependency_named_root_remains_candidate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text(".venv/*\n", encoding="utf-8")
            tracked = root / ".venv/project_source.py"
            tracked.parent.mkdir(parents=True)
            tracked.write_text("VALUE = 1\n", encoding="utf-8")
            ignored = root / ".venv/runtime_extension.py"
            ignored.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "add", "-f", ".venv/project_source.py"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "versioned dependency-named source root"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            original = current_candidate_sha(root)
            ignored.write_text("VALUE = 2\n", encoding="utf-8")
            changed = current_candidate_sha(root)

        self.assertNotEqual(original, changed)

    def test_candidate_identity_binds_gitignore_and_non_generated_reserved_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            gitignore = root / ".gitignore"
            gitignore.write_text("generated/\n", encoding="utf-8")
            loader = root / "loader.py"
            loader.write_text(
                "from pathlib import Path\n"
                "CONFIG = Path('.gitignore').read_text()\n"
                "EXTENSION = Path('docs/harness/runtime_extension.py').read_text()\n",
                encoding="utf-8",
            )
            harness_extension = root / "docs/harness/runtime_extension.py"
            harness_extension.parent.mkdir(parents=True)
            harness_extension.write_text("VALUE = 1\n", encoding="utf-8")
            agent_extension = root / ".codex/agents/runtime_extension.py"
            agent_extension.parent.mkdir(parents=True)
            agent_extension.write_text("VALUE = 1\n", encoding="utf-8")
            generated_projection = root / "docs/harness/delivery.md"
            generated_projection.write_text("generated v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "candidate source and generated view"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            original = current_candidate_sha(root)

            generated_projection.write_text("generated v2\n", encoding="utf-8")
            generated_only = current_candidate_sha(root)
            self.assertEqual(original, generated_only)

            gitignore.write_text("different-generated/\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "change runtime-readable gitignore"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            gitignore_changed = current_candidate_sha(root)

            harness_extension.write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "docs/harness/runtime_extension.py"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "change non-generated harness sibling"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            harness_extension_changed = current_candidate_sha(root)

            agent_extension.write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".codex/agents/runtime_extension.py"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "change non-generated agent sibling"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            agent_extension_changed = current_candidate_sha(root)

        self.assertNotEqual(original, gitignore_changed)
        self.assertNotEqual(gitignore_changed, harness_extension_changed)
        self.assertNotEqual(harness_extension_changed, agent_extension_changed)

    def test_no_git_candidate_identity_fails_closed_on_non_regular_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            non_regular = root / "runtime.pipe"
            non_regular.write_bytes(b"placeholder")
            original_is_file = Path.is_file
            original_is_dir = Path.is_dir

            def report_non_regular_file(path: Path) -> bool:
                if path.resolve() == non_regular.resolve():
                    return False
                return original_is_file(path)

            def report_non_regular_directory(path: Path) -> bool:
                if path.resolve() == non_regular.resolve():
                    return False
                return original_is_dir(path)

            with (
                patch.object(Path, "is_file", new=report_non_regular_file),
                patch.object(Path, "is_dir", new=report_non_regular_directory),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-regular"):
                    current_candidate_sha(root)

    def test_candidate_identity_binds_executable_mode_and_file_framing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "non-executable candidate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            non_executable = current_candidate_sha(root)
            source.chmod(0o755)
            subprocess.run(
                ["git", "update-index", "--chmod=+x", "candidate.py"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "executable candidate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            executable = current_candidate_sha(root)

            first = root / "tests/a"
            second = root / "tests/b"
            first.parent.mkdir()
            first.write_bytes(b"X\0tests/b\0" + b"100644" + b"\0Y")
            subprocess.run(["git", "add", "tests/a"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "one framed file"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            one_file = current_candidate_sha(root)
            first.write_bytes(b"X")
            second.write_bytes(b"Y")
            subprocess.run(["git", "add", "tests/a", "tests/b"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "two framed files"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            two_files = current_candidate_sha(root)

        self.assertNotEqual(non_executable, executable)
        self.assertNotEqual(one_file, two_files)

    def test_candidate_identity_rejects_symlink_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            original_is_symlink = Path.is_symlink

            def report_candidate_as_symlink(path: Path) -> bool:
                if path.resolve() == source.resolve():
                    return True
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", new=report_candidate_as_symlink):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    current_candidate_sha(root)

    def test_candidate_identity_rejects_head_only_gitlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Kafa Test"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "tests/source.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{commit},tests/submodule",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "record gitlink"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "rm", "--cached", "tests/submodule"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            with self.assertRaisesRegex(RuntimeError, "non-regular.*tests/submodule"):
                current_candidate_sha(root)

    def test_candidate_identity_ignores_commit_replace_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Kafa Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "tests/source.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "clean baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            clean_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{clean_commit},tests/submodule",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "head contains gitlink"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            replaced_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "rm", "--cached", "tests/submodule"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "replace", replaced_head, clean_commit],
                cwd=root,
                check=True,
            )

            with self.assertRaisesRegex(RuntimeError, "non-regular.*tests/submodule"):
                current_candidate_sha(root)

    def test_candidate_identity_rejects_missing_blob_hidden_by_replace_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Kafa Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "candidate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            object_id = subprocess.run(
                ["git", "rev-parse", "HEAD:candidate.py"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            replacement = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=root,
                input="replacement bytes\n",
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "replace", object_id, replacement],
                cwd=root,
                check=True,
            )
            loose_object = root / ".git/objects" / object_id[:2] / object_id[2:]
            self.assertTrue(loose_object.is_file())
            unlink_git_object(loose_object)

            with self.assertRaisesRegex(RuntimeError, "Git object"):
                current_candidate_sha(root)

    def test_candidate_identity_ignores_ambient_git_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ambient = Path(temp) / "ambient"
            root.mkdir()
            ambient.mkdir()
            for repository in (root, ambient):
                subprocess.run(
                    ["git", "init"],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            expected = current_candidate_sha(root)

            with patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(ambient / ".git"),
                    "GIT_WORK_TREE": str(ambient),
                },
                clear=False,
            ):
                isolated = current_candidate_sha(root)

        self.assertEqual(isolated, expected)

    def test_candidate_identity_pins_root_against_local_core_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            redirected = Path(temp) / "redirected-worktree"
            root.mkdir()
            redirected.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "core.worktree", str(redirected)],
                cwd=root,
                check=True,
            )

            before = harness_lib.git_source_tree_hash(root)
            hidden = root / "plugins/evil.py"
            hidden.parent.mkdir(parents=True)
            hidden.write_text("raise RuntimeError('must be bound')\n", encoding="utf-8")
            after = harness_lib.git_source_tree_hash(root)
            dirty = harness_lib.git_dirty(root)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(after, before)
        self.assertIs(dirty, True)

    def test_candidate_identity_fails_closed_on_missing_tracked_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "kafa@example.invalid"],
                cwd=root,
                check=True,
            )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "candidate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            object_id = subprocess.run(
                ["git", "rev-parse", "HEAD:candidate.py"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            loose_object = root / ".git/objects" / object_id[:2] / object_id[2:]
            self.assertTrue(loose_object.is_file())
            unlink_git_object(loose_object)

            with self.assertRaisesRegex(RuntimeError, "Git object"):
                current_candidate_sha(root)

    def test_delivery_record_rejects_candidate_change_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_delivery_fixture(root)
            validated_candidate = current_candidate_sha(root)

            with patch.object(
                harness_db,
                "current_candidate_sha",
                side_effect=(validated_candidate, "changed-after-validation"),
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "stale candidate"):
                    harness_db.record_delivery(root, "candidate")

            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute(
                        "select status from delivery_cycles where id = 'CYCLE-current'"
                    ).fetchone()[0],
                    "active",
                )

    def test_validation_execution_link_rejects_mismatched_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_delivery_fixture(root)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("pragma foreign_keys = on")
                conn.execute(
                    """insert into executions
                    select 'EX-other', cycle_id, 'other-candidate', target_id, command,
                           exit_code, stdout_sha256, artifact_path, executed_count,
                           result_format, semantic_status, runner, sandbox_status,
                           no_network, policy_status, created_at
                    from executions where id = 'EX1'"""
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "insert into validation_executions (validation_id, execution_id) "
                        "values ('V1', 'EX-other')"
                    )

    def test_direct_db_tampering_and_forged_review_metadata_cannot_bypass_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_delivery_fixture(
                root,
                failure_mode_status="identified",
            )
            with closing(sqlite3.connect(db)) as conn:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("update executions set semantic_status='pass' where id='EX1'")
                conn.execute(
                    """
                    update quality_gates
                    set result='pass', review_status='reviewed-local',
                        producer_context_id='forged-producer',
                        reviewer_context_id='forged-reviewer'
                    where id='G1'
                    """
                )
                conn.execute(
                    """
                    insert into decisions
                    (id, cycle_id, candidate_sha, decision, reason, created_at)
                    select 'D-forged', 'CYCLE-current', candidate_sha,
                           'ci-success-hmac-valid', 'hmac:' || printf('%064d', 0),
                           '2026-07-11T00:00:00Z'
                    from delivery_cycles where id='CYCLE-current'
                    """
                )
                conn.commit()

            issues = schema30_issues(root)

        self.assertIn("human-review-required", " ".join(issues))


if __name__ == "__main__":
    unittest.main()

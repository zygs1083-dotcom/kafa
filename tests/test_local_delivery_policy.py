from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sqlite3
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
from core.schema_lifecycle import create_schema30  # noqa: E402
import harness_db  # noqa: E402


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def prepare_cli_validation(root: Path) -> None:
    (root / ".gitignore").write_text(
        "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
        encoding="utf-8",
    )
    harness_db.render_all(root)


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
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
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
            (root / "dirty.txt").write_text("not reviewed\n", encoding="utf-8")

            issues = schema30_issues(root)

        self.assertIn("git worktree is dirty after quality gate", " ".join(issues))

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

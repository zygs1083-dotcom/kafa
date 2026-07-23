from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import delivery as delivery_policy  # noqa: E402
from core import projections as projection_views  # noqa: E402
from core.projections import projection_content_issues  # noqa: E402
import harness_db  # noqa: E402


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def write_structured_target(root: Path) -> None:
    (root / "emit_structured_result.py").write_text(
        "from pathlib import Path\n"
        "result = Path('.ai-team/runtime/pytest.json')\n"
        "result.parent.mkdir(parents=True, exist_ok=True)\n"
        "result.write_text("
        "'{\"summary\":{\"total\":1,\"passed\":1,\"failed\":0,\"errors\":0}}',"
        " encoding='utf-8')\n"
        "print('structured result written')\n",
        encoding="utf-8",
    )


def write_regex_target(root: Path) -> None:
    (root / "test_medium_regex.py").write_text(
        "import unittest\n\n"
        "class MediumRegexTest(unittest.TestCase):\n"
        "    def test_current_candidate(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def current_revision(root: Path) -> int:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return int(conn.execute("select revision from project where id=1").fetchone()[0])


def build_medium_graph(
    root: Path,
    *,
    failure_mode_status: str | None = None,
    cover_failure_mode: bool = False,
    structured: bool = True,
    degraded: bool = False,
    residual_risk: str = "explicit local residual risk",
    failure_mode_risk: str = "medium",
) -> str:
    harness_db.init_runtime(root)
    if structured:
        write_structured_target(root)
        command = "python3 emit_structured_result.py"
        kind = "build"
        result_format = "pytest-json"
        result_path = ".ai-team/runtime/pytest.json"
    else:
        write_regex_target(root)
        command = "python3 -B -m unittest test_medium_regex.py"
        kind = "unit"
        result_format = "regex"
        result_path = ""
    harness_db.add_requirement(root, "REQ1", "functional", "structured delivery")
    harness_db.add_acceptance(root, "AC1", "structured result is current")
    harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
    harness_db.add_test_target(
        root,
        "STRUCT",
        kind,
        command,
        "structured current-candidate target",
        result_format=result_format,
        result_path=result_path,
    )
    harness_db.add_task(root, "T1", "produce structured evidence", acceptance="AC1")
    harness_db.link_task_test_target(root, "T1", "STRUCT")
    qualification_id = harness_db.qualify_test_target(
        root,
        "Q1",
        "STRUCT",
        "AC1",
        "STRUCT directly exercises the acceptance",
        "test-controller",
    )
    if failure_mode_status is not None:
        accepted = failure_mode_status in {"accepted", "exempt"}
        harness_db.add_failure_mode(
            root,
            "FM-MEDIUM",
            "delivery",
            "medium risk",
            "delivery attempt",
            "fail closed or carry explicit acceptance",
            risk=failure_mode_risk,
            status=failure_mode_status,
            acceptance="AC1",
            accepted_by="risk-owner" if accepted else "",
            acceptance_reason="candidate-scoped residual risk" if accepted else "",
            acceptance_scope="current local candidate" if accepted else "",
            expires_at="2099-01-01T00:00:00Z" if accepted else "",
        )
    harness_db.start_task(root, "T1")
    harness_db.verify_run(
        root,
        "STRUCT",
        acceptance="AC1",
        failure_modes=["FM-MEDIUM"] if cover_failure_mode else [],
    )
    harness_db.submit_task(
        root,
        "T1",
        "immutable structured execution",
        context_id="producer-context",
    )
    harness_db.accept_task(root, "T1", "reviewed structured evidence")
    harness_db.confirm_baseline(root, "B1", "current risk graph", by="test-controller")
    if degraded:
        harness_db.record_gate(
            root,
            "same-context-degraded",
            "pass",
            residual_risk=residual_risk,
            qualifications=[qualification_id],
        )
    else:
        harness_db.record_gate(
            root,
            "fresh",
            "pass",
            reviewer_context_id="reviewer-context",
            residual_risk=residual_risk,
            qualifications=[qualification_id],
        )
    return qualification_id


def delivery_report(root: Path):
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.row_factory = sqlite3.Row
        return delivery_policy.evaluate_delivery_report(
            conn,
            root,
            mode="enter-readiness",
            is_expired=harness_db.is_expired,
            observed_at="2026-07-20T00:00:00Z",
        )


def medium_acceptance_decision(
    *,
    review_status: str = "reviewed-local",
    producer_context_id: str = "producer-context",
    reviewer_context_id: str = "reviewer-context",
    residual_risk: str = "explicit residual risk",
    **overrides: object,
):
    acceptance: dict[str, object] = {
        "risk_id": "FM-MEDIUM",
        "risk": "medium",
        "status": "accepted",
        "actor": "risk-owner",
        "reason": "candidate-scoped acceptance",
        "scope": "current candidate",
        "revision": 7,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    acceptance.update(overrides)
    return delivery_policy.evaluate_local_trust(
        risk_levels={"medium"},
        structured_current_execution=True,
        producer_context_id=producer_context_id,
        reviewer_context_id=reviewer_context_id,
        review_status=review_status,
        residual_risk=residual_risk,
        risk_acceptances=[acceptance],
        required_risk_ids={"FM-MEDIUM"},
        current_revision=7,
        now="2026-07-20T00:00:00Z",
    )


class MediumRiskPolicyRedTests(unittest.TestCase):
    def test_uncovered_medium_failure_mode_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, failure_mode_status="identified")

            report = delivery_report(root)

        self.assertIn(
            "medium-failure-mode-uncovered",
            [blocker.code for blocker in report.blockers],
        )
        self.assertFalse(report.trust.delivery_allowed)

    def test_open_medium_finding_blocks_even_with_passing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root)
            harness_db.record_finding(
                root,
                "F-MEDIUM",
                "delivery",
                "medium",
                "open",
                "unresolved medium finding",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-context-2",
                residual_risk="finding remains unresolved",
                findings="F-MEDIUM",
                qualifications=[qualification_id],
            )

            report = delivery_report(root)

        self.assertIn(
            "medium-finding-open",
            [blocker.code for blocker in report.blockers],
        )
        self.assertFalse(report.trust.delivery_allowed)

    def test_incomplete_medium_acceptance_fails_closed(self) -> None:
        decision = medium_acceptance_decision(reason="")
        self.assertFalse(decision.delivery_allowed)
        self.assertEqual(decision.status, "human-review-required")
        self.assertIn("incomplete", " ".join(decision.reasons))

    def test_expired_medium_acceptance_fails_closed(self) -> None:
        decision = medium_acceptance_decision(
            expires_at="2026-07-19T00:00:00Z"
        )
        self.assertFalse(decision.delivery_allowed)
        self.assertEqual(decision.status, "human-review-required")
        self.assertIn("expired", " ".join(decision.reasons))

    def test_stale_medium_acceptance_fails_closed(self) -> None:
        decision = medium_acceptance_decision(revision=6)
        self.assertFalse(decision.delivery_allowed)
        self.assertEqual(decision.status, "human-review-required")
        self.assertIn("stale", " ".join(decision.reasons))

    def test_empty_degraded_residual_risk_is_rejected_before_gate_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root, degraded=False)
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = int(conn.execute("select count(*) from quality_gates").fetchone()[0])

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "residual-risk",
            ):
                harness_db.record_gate(
                    root,
                    "same-context-degraded",
                    "pass",
                    residual_risk="",
                    qualifications=[qualification_id],
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                after = int(conn.execute("select count(*) from quality_gates").fetchone()[0])
            self.assertEqual(after, before)

    def test_derived_degraded_gate_also_rejects_empty_residual_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update tasks set submitted_context_id='' where id='T1'"
                )
                before = int(
                    conn.execute("select count(*) from quality_gates").fetchone()[0]
                )
                conn.commit()

            with self.assertRaisesRegex(harness_db.HarnessError, "residual-risk"):
                harness_db.record_gate(
                    root,
                    "fresh",
                    "pass",
                    reviewer_context_id="reviewer-with-no-producer",
                    residual_risk="",
                    qualifications=[qualification_id],
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                after = int(
                    conn.execute("select count(*) from quality_gates").fetchone()[0]
                )
            self.assertEqual(after, before)

    def test_tampered_empty_degraded_residual_risk_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, degraded=True)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update quality_gates set residual_risk='' "
                    "where gate_status='active'"
                )
                conn.commit()

            report = delivery_report(root)

        self.assertIn(
            "degraded-residual-risk-missing",
            [blocker.code for blocker in report.blockers],
        )
        self.assertFalse(report.trust.delivery_allowed)

    def test_regex_execution_does_not_cover_identified_medium_failure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
                structured=False,
            )

            report = delivery_report(root)
            projection = (
                root / ".ai-team/requirements/failure-modes.md"
            ).read_text(encoding="utf-8")
            projection_issues = projection_content_issues(root)

        blockers = [
            blocker
            for blocker in report.blockers
            if blocker.code == "medium-failure-mode-uncovered"
        ]
        self.assertEqual(
            [(blocker.entity_type, blocker.entity_id) for blocker in blockers],
            [("failure_mode", "FM-MEDIUM")],
        )
        row_text = next(
            line for line in projection.splitlines() if line.startswith("| FM-MEDIUM ")
        )
        cells = [cell.strip() for cell in row_text.strip("|").split("|")]
        self.assertEqual(cells[10], "")
        self.assertEqual(projection_issues, [])

    def test_stale_candidate_validation_does_not_cover_failure_mode_after_lazy_filter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
            )
            source = root / "emit_structured_result.py"
            source.write_text(
                source.read_text(encoding="utf-8") + "# stale\n",
                encoding="utf-8",
            )
            from core.cycle_ledger import current_candidate_sha

            with patch(
                "core.cycle_ledger.current_candidate_sha",
                wraps=current_candidate_sha,
            ) as candidate:
                projection_views.render_failure_modes(root)

            row = next(
                line
                for line in (
                    root / ".ai-team/requirements/failure-modes.md"
                ).read_text(encoding="utf-8").splitlines()
                if line.startswith("| FM-MEDIUM ")
            )

        self.assertEqual(
            [cell.strip() for cell in row.strip("|").split("|")][10],
            "",
        )
        self.assertEqual(candidate.call_count, 1)

    def test_failure_mode_coverage_must_use_its_linked_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            write_structured_target(root)
            harness_db.add_acceptance(root, "AC1", "first behavior")
            harness_db.add_acceptance(root, "AC2", "second behavior")
            harness_db.add_test_target(
                root,
                "STRUCT",
                "build",
                "python3 emit_structured_result.py",
                result_format="pytest-json",
                result_path=".ai-team/runtime/pytest.json",
            )
            harness_db.qualify_test_target(
                root,
                "Q1",
                "STRUCT",
                "AC1",
                "target is qualified only for AC1",
                "test-controller",
            )
            harness_db.add_failure_mode(
                root,
                "FM-MEDIUM",
                "delivery",
                "acceptance mismatch",
                "verification",
                "reject unrelated evidence",
                risk="medium",
                acceptance="AC2",
            )

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "failure-mode coverage acceptance is not linked",
            ):
                harness_db.verify_run(
                    root,
                    "STRUCT",
                    acceptance="AC1",
                    failure_modes=["FM-MEDIUM"],
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from executions").fetchone()[0],
                    0,
                )

    def test_each_uncovered_medium_failure_mode_has_its_own_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(
                root,
                failure_mode_status="identified",
            )
            harness_db.add_failure_mode(
                root,
                "FM-MEDIUM-2",
                "delivery",
                "second medium risk",
                "delivery attempt",
                "fail closed",
                risk="medium",
                acceptance="AC1",
            )
            harness_db.confirm_baseline(
                root,
                "B2",
                "two current medium risks",
                by="test-controller",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-context-2",
                residual_risk="both risks require coverage",
                qualifications=[qualification_id],
            )

            report = delivery_report(root)

        blockers = [
            blocker
            for blocker in report.blockers
            if blocker.code == "medium-failure-mode-uncovered"
        ]
        self.assertEqual(
            {(blocker.entity_type, blocker.entity_id) for blocker in blockers},
            {
                ("failure_mode", "FM-MEDIUM"),
                ("failure_mode", "FM-MEDIUM-2"),
            },
        )

    def test_complete_medium_acceptance_cannot_bypass_review_identity(self) -> None:
        invalid_status = medium_acceptance_decision(review_status="unknown")
        same_context = medium_acceptance_decision(
            producer_context_id="same-context",
            reviewer_context_id="same-context",
        )
        for decision in (invalid_status, same_context):
            self.assertFalse(decision.delivery_allowed)
            self.assertEqual(decision.status, "human-review-required")

    def test_expired_accepted_medium_finding_is_not_reported_as_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root)
            harness_db.record_finding(
                root,
                "F-MEDIUM",
                "delivery",
                "medium",
                "accepted",
                "expired accepted medium finding",
                waived_by="risk-owner",
                waiver_reason="temporary acceptance",
                waiver_scope="current candidate",
                waived_revision=current_revision(root),
                waiver_expires_at="2026-07-19T00:00:00Z",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-context-2",
                residual_risk="expired acceptance must block",
                findings="F-MEDIUM",
                qualifications=[qualification_id],
            )

            report = delivery_report(root)

        codes = [blocker.code for blocker in report.blockers]
        self.assertIn("risk-acceptance-invalid", codes)
        self.assertNotIn("medium-finding-open", codes)


class MediumRiskPolicyPositiveTests(unittest.TestCase):
    def test_qualified_structured_medium_coverage_allows_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
            )

            self.assertEqual(projection_content_issues(root), [])

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "qualified medium coverage")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("delivered",))

    def test_projection_verifier_fails_closed_if_artifact_changes_mid_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                artifact = root / conn.execute(
                    "select artifact_path from executions order by created_at limit 1"
                ).fetchone()[0]
            original_render = projection_views.render_failure_modes
            changed = False

            def render_then_change(*args, **kwargs):
                nonlocal changed
                result = original_render(*args, **kwargs)
                if kwargs.get("candidate_override") and not changed:
                    artifact.write_bytes(b"tampered after evidence snapshot\n")
                    changed = True
                return result

            with patch.object(
                projection_views,
                "render_failure_modes",
                side_effect=render_then_change,
            ):
                issues = projection_views.projection_content_issues(root)

        self.assertTrue(changed)
        self.assertTrue(
            any("execution artifact changed during projection verification" in issue for issue in issues),
            issues,
        )

    def test_projection_verifier_fails_closed_if_candidate_changes_mid_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
            )
            candidate_source = root / "emit_structured_result.py"
            original_render = projection_views.render_failure_modes
            changed = False

            def render_then_change(*args, **kwargs):
                nonlocal changed
                result = original_render(*args, **kwargs)
                if kwargs.get("candidate_override") and not changed:
                    candidate_source.write_text(
                        candidate_source.read_text(encoding="utf-8")
                        + "# changed during projection verification\n",
                        encoding="utf-8",
                    )
                    changed = True
                return result

            with patch.object(
                projection_views,
                "render_failure_modes",
                side_effect=render_then_change,
            ):
                issues = projection_views.projection_content_issues(root)

        self.assertTrue(changed)
        self.assertTrue(
            any("candidate changed during projection verification" in issue for issue in issues),
            issues,
        )

    def test_projection_verifier_never_reads_escaping_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
            )
            outside = base / "outside-artifact.txt"
            outside.write_bytes(b"outside authority must remain untouched\n")
            before = outside.read_bytes()
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger executions_no_update")
                conn.execute(
                    "update executions set artifact_path='../outside-artifact.txt'"
                )
                conn.commit()
            projection_views.render_failure_modes(root)

            issues = projection_views.projection_content_issues(root)

            self.assertTrue(
                any("could not be snapshotted" in issue for issue in issues),
                issues,
            )
            self.assertEqual(outside.read_bytes(), before)

    def test_complete_current_medium_acceptance_is_procedural_accepted_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, failure_mode_status="accepted")

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium residual risk")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
                accepted_revision = int(
                    conn.execute(
                        "select accepted_revision from failure_modes where id='FM-MEDIUM'"
                    ).fetchone()[0]
                )
            self.assertEqual(row, ("accepted-risk",))
            self.assertEqual(accepted_revision, current_revision(root))
            projection = (
                root / ".ai-team/requirements/failure-modes.md"
            ).read_text(encoding="utf-8")
            row_text = next(
                line for line in projection.splitlines() if line.startswith("| FM-MEDIUM ")
            )
            cells = [cell.strip() for cell in row_text.strip("|").split("|")]
            self.assertEqual(int(cells[14]), accepted_revision)
            with closing(sqlite3.connect(db_path(root))) as conn:
                self.assertEqual(
                    conn.execute(
                        "select count(*) from events "
                        "where event_type='risk_acceptance_rebound' "
                        "and entity_id='FM-MEDIUM'"
                    ).fetchone()[0],
                    1,
                )

    def test_complete_current_medium_exemption_is_procedural_accepted_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, failure_mode_status="exempt")

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "exempt medium residual risk")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("accepted-risk",))

    def test_accepted_medium_risk_does_not_require_structured_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="accepted",
                structured=False,
            )

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium regex evidence")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("accepted-risk",))

    def test_identified_low_risk_retains_qualified_regex_delivery_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="identified",
                cover_failure_mode=True,
                structured=False,
                failure_mode_risk="low",
            )

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "qualified low-risk regex evidence")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("delivered",))

    def test_complete_current_medium_finding_acceptance_is_procedural(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root)
            harness_db.record_finding(
                root,
                "F-MEDIUM",
                "delivery",
                "medium",
                "accepted",
                "explicitly accepted medium finding",
                waived_by="risk-owner",
                waiver_reason="candidate-scoped acceptance",
                waiver_scope="current candidate",
                waived_revision=current_revision(root),
                waiver_expires_at="2099-01-01T00:00:00Z",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-context-2",
                residual_risk="accepted medium finding remains procedural",
                findings="F-MEDIUM",
                qualifications=[qualification_id],
            )

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium finding")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("accepted-risk",))

    def test_resolved_and_false_positive_medium_findings_do_not_block(self) -> None:
        for status in ("resolved", "false-positive"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                qualification_id = build_medium_graph(root)
                harness_db.record_finding(
                    root,
                    "F-MEDIUM",
                    "delivery",
                    "medium",
                    status,
                    f"medium finding is {status}",
                )
                harness_db.record_gate(
                    root,
                    "fresh",
                    "pass",
                    reviewer_context_id="reviewer-context-2",
                    residual_risk="no remaining medium finding",
                    findings="F-MEDIUM",
                    qualifications=[qualification_id],
                )

                harness_db.enter_delivery_readiness(root)
                harness_db.record_delivery(root, f"medium finding {status}")

                with closing(sqlite3.connect(db_path(root))) as conn:
                    row = conn.execute(
                        "select decision_status from deliveries order by created_at desc limit 1"
                    ).fetchone()
                self.assertEqual(row, ("delivered",))

    def test_medium_acceptance_cannot_waive_other_delivery_prerequisites(self) -> None:
        cases = (
            ("graph", "requirement-missing"),
            ("qualification", "qualification-stale"),
            ("task", "accepted-task-missing"),
            ("candidate", "current-validation-missing"),
            ("execution", "current-execution-missing"),
            ("gate", "quality-gate-invalid"),
        )
        for mutation, expected in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                build_medium_graph(root, failure_mode_status="accepted")
                if mutation == "candidate":
                    (root / "candidate-change.txt").write_text(
                        "changed after gate\n", encoding="utf-8"
                    )
                else:
                    with closing(sqlite3.connect(db_path(root))) as conn:
                        if mutation == "graph":
                            conn.execute(
                                "update requirements set status='cancelled' where id='REQ1'"
                            )
                        elif mutation == "qualification":
                            conn.execute(
                                "update acceptance set revision=revision+1 where id='AC1'"
                            )
                        elif mutation == "task":
                            conn.execute(
                                "update tasks set status='cancelled' where id='T1'"
                            )
                        elif mutation == "execution":
                            conn.execute("drop trigger executions_no_update")
                            conn.execute("pragma ignore_check_constraints=on")
                            conn.execute(
                                "update executions set target_definition_sha256=''"
                            )
                        elif mutation == "gate":
                            conn.execute(
                                "update quality_gates set result='fail' "
                                "where gate_status='active'"
                            )
                        conn.commit()

                codes = [blocker.code for blocker in delivery_report(root).blockers]
                self.assertIn(expected, codes)

    def test_low_risk_degraded_review_with_notes_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                degraded=True,
                residual_risk="explicit low-risk same-context limitation",
            )

            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "low-risk degraded path")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select decision_status from deliveries order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(row, ("same-context-degraded",))

    def test_high_risk_degraded_review_remains_human_review_required(self) -> None:
        decision = delivery_policy.evaluate_local_trust(
            risk_levels={"high"},
            structured_current_execution=True,
            producer_context_id="producer-context",
            reviewer_context_id="reviewer-context",
            review_status="same-context-degraded",
            risk_acceptances=[
                {
                    "risk_id": "FM-HIGH",
                    "risk": "high",
                    "status": "accepted",
                    "actor": "risk-owner",
                    "reason": "explicit risk acceptance",
                    "scope": "current candidate",
                    "revision": 7,
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ],
            required_risk_ids={"FM-HIGH"},
            current_revision=7,
            now="2026-07-20T00:00:00Z",
        )
        self.assertFalse(decision.delivery_allowed)
        self.assertEqual(decision.status, "human-review-required")


class HistoricalRiskPolicyReplayTests(unittest.TestCase):
    def test_historical_audit_rechecks_medium_failure_mode_acceptance_expiry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, failure_mode_status="accepted")
            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium failure mode")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            self.assertTrue(
                harness_db.cycle_audit(root, "CYCLE-current")["consistent"]
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update failure_modes set expires_at='2000-01-01T00:00:00Z' "
                    "where cycle_id='CYCLE-current' and id='FM-MEDIUM'"
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "risk-acceptance-invalid",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )

    def test_historical_audit_rechecks_medium_finding_acceptance_expiry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = build_medium_graph(root)
            harness_db.record_finding(
                root,
                "F-MEDIUM",
                "delivery",
                "medium",
                "accepted",
                "explicitly accepted medium finding",
                waived_by="risk-owner",
                waiver_reason="candidate-scoped acceptance",
                waiver_scope="current candidate",
                waived_revision=current_revision(root),
                waiver_expires_at="2099-01-01T00:00:00Z",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-context-2",
                residual_risk="accepted medium finding remains procedural",
                findings="F-MEDIUM",
                qualifications=[qualification_id],
            )
            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium finding")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            self.assertTrue(
                harness_db.cycle_audit(root, "CYCLE-current")["consistent"]
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update findings "
                    "set waiver_expires_at='2000-01-01T00:00:00Z' "
                    "where cycle_id='CYCLE-current' and id='F-MEDIUM'"
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "risk-acceptance-invalid",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )


if __name__ == "__main__":
    unittest.main()

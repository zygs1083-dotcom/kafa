from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Callable
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core import delivery as delivery_core  # noqa: E402
from core.invariant_checker import check_cycle_invariants  # noqa: E402
from core.projections import projection_content_issues  # noqa: E402


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def query_rows(
    root: Path,
    sql: str,
    values: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return [tuple(row) for row in conn.execute(sql, values).fetchall()]


def write_passing_target(root: Path) -> str:
    (root / "test_candidate.py").write_text(
        "import unittest\n\n"
        "class CandidateTest(unittest.TestCase):\n"
        "    def test_candidate(self):\n"
        "        self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8",
    )
    return "python3 -B -m unittest test_candidate.py"


def prepare_ready_graph(
    root: Path,
    *,
    base_ref: str = "",
    before_verification: Callable[[], None] | None = None,
    second_acceptance: bool = False,
    failure_mode: bool = False,
) -> None:
    harness_db.init_runtime(root)
    if base_ref:
        with closing(sqlite3.connect(db_path(root))) as conn:
            conn.execute(
                "update delivery_cycles set base_ref=? where id='CYCLE-current'",
                (base_ref,),
            )
            conn.commit()
    command = write_passing_target(root)
    graph = [("REQ1", "AC1", "T1", "Q1")]
    if second_acceptance:
        graph.append(("REQ2", "AC2", "T2", "Q2"))
    for requirement_id, acceptance_id, _, _ in graph:
        harness_db.add_requirement(
            root,
            requirement_id,
            "functional",
            f"candidate works for {acceptance_id}",
        )
        harness_db.add_acceptance(
            root,
            acceptance_id,
            f"candidate test passes for {acceptance_id}",
        )
        harness_db.link_requirement_acceptance(
            root,
            requirement_id,
            acceptance_id,
        )
    if failure_mode:
        harness_db.add_failure_mode(
            root,
            "FM1",
            "candidate",
            "candidate failure",
            "candidate input",
            "candidate remains correct",
            risk="low",
            acceptance="AC1",
        )
    for _, acceptance_id, task_id, _ in graph:
        harness_db.add_task(
            root,
            task_id,
            f"implement {acceptance_id}",
            acceptance=acceptance_id,
            failure_modes="FM1" if failure_mode and acceptance_id == "AC1" else "",
        )
    harness_db.add_test_target(
        root,
        "UNIT",
        "unit",
        command,
        "candidate unit target",
    )
    qualification_ids: list[str] = []
    for _, acceptance_id, task_id, qualification_id in graph:
        harness_db.link_task_test_target(root, task_id, "UNIT")
        qualification_ids.append(
            harness_db.qualify_test_target(
                root,
                qualification_id,
                "UNIT",
                acceptance_id,
                f"UNIT directly exercises {acceptance_id}",
                "root-controller",
            )
        )
    if before_verification is not None:
        before_verification()
    harness_db.confirm_baseline(
        root,
        "BL1",
        "REQ1 and AC1 are the confirmed scope",
        by="root-controller",
    )
    for _, acceptance_id, task_id, _ in graph:
        harness_db.start_task(root, task_id)
        harness_db.verify_run(
            root,
            "UNIT",
            acceptance=acceptance_id,
            failure_modes=(
                ["FM1"] if failure_mode and acceptance_id == "AC1" else []
            ),
        )
        harness_db.submit_task(
            root,
            task_id,
            "root inspected the current candidate",
            context_id="producer-context",
        )
        harness_db.accept_task(
            root,
            task_id,
            "current execution and review complete",
        )
    harness_db.record_gate(
        root,
        "fresh",
        "pass",
        reviewer_context_id="reviewer-context",
        qualifications=qualification_ids,
    )
    harness_db.enter_delivery_readiness(root)


class DerivedDeliveryNarrativeRedTests(unittest.TestCase):
    def test_scope_only_delivery_links_all_proven_acceptances(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)

            harness_db.record_delivery(
                root,
                "verified local patch",
                handoff="return code and residual risks to the user",
            )

            self.assertEqual(
                query_rows(
                    root,
                    "select acceptance_id from delivery_acceptance order by acceptance_id",
                ),
                [("AC1",)],
            )
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            self.assertIn(
                delivery_id,
                (root / ".ai-team/requirements/traceability.md").read_text(
                    encoding="utf-8"
                ),
            )

    def test_contradictory_legacy_prose_is_supplemental_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)

            harness_db.record_delivery(
                root,
                "verified local patch",
                acceptance="FAKE-AC",
                validation="no tests ran",
                qa="quality gate failed",
                quality_gate="unreviewed",
                handoff="return verified code",
            )

            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            authoritative, supplemental = projection.split(
                "### Legacy / Supplemental Notes", 1
            )
            self.assertIn("AC1", authoritative)
            self.assertIn("UNIT", authoritative)
            self.assertIn("Q1", authoritative)
            self.assertNotIn("FAKE-AC", authoritative)
            self.assertNotIn("no tests ran", authoritative)
            self.assertNotIn("quality gate failed", authoritative)
            self.assertIn("FAKE-AC", supplemental)
            self.assertIn("no tests ran", supplemental)
            self.assertIn("quality gate failed", supplemental)
            self.assertEqual(
                query_rows(
                    root,
                    "select acceptance_id from delivery_acceptance order by acceptance_id",
                ),
                [("AC1",)],
            )

    def test_authoritative_validation_does_not_depend_on_fixed_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)

            harness_db.record_delivery(root, "verified local patch")

            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            authoritative = projection.split("### Legacy / Supplemental Notes", 1)[0]
            self.assertIn("Execution IDs", authoritative)
            self.assertIn("Validation IDs", authoritative)
            self.assertIn("Target IDs", authoritative)
            self.assertNotIn("controller execution passed", authoritative)

    def test_judgment_only_validation_is_not_execution_backed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_validation(
                root,
                "manual-review",
                "human judgment without a controller execution",
                "pass",
                acceptance="AC1",
            )
            judgment_id = str(
                query_rows(
                    root,
                    "select id from validations where surface='manual-review'",
                )[0][0]
            )

            harness_db.record_delivery(root, "verified local patch")

            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            authoritative = projection.split("### Legacy / Supplemental Notes", 1)[0]
            execution_backed, judgment_only = authoritative.split(
                "### Judgment-only Validations", 1
            )
            self.assertNotIn(judgment_id, execution_backed)
            self.assertIn(judgment_id, judgment_only)
            self.assertIn("not execution evidence", judgment_only.lower())

    def test_judgment_only_failure_mode_link_is_not_execution_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root, failure_mode=True)
            harness_db.record_validation(
                root,
                "manual-failure-review",
                "human judgment only",
                "pass",
                acceptance="AC1",
                failure_modes="FM1",
            )
            judgment_id = str(
                query_rows(
                    root,
                    "select id from validations where surface='manual-failure-review'",
                )[0][0]
            )

            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            with harness_db.connection(root) as conn:
                facts = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            failure_mode = next(
                fact for fact in facts.failure_mode_facts if fact.id == "FM1"
            )
            self.assertIn(judgment_id, facts.judgment_validation_ids)
            self.assertNotIn(judgment_id, failure_mode.validation_ids)
            self.assertEqual(len(failure_mode.validation_ids), 1)

    def test_accepted_medium_regex_is_not_structured_failure_mode_coverage(self) -> None:
        from tests.test_delivery_integrity_p1_contracts import build_medium_graph

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(
                root,
                failure_mode_status="accepted",
                cover_failure_mode=True,
                structured=False,
            )
            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium regex evidence")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            linked_validation_id = str(
                query_rows(
                    root,
                    "select validation_id from validation_failure_modes "
                    "where failure_mode_id='FM-MEDIUM'",
                )[0][0]
            )

            with harness_db.connection(root) as conn:
                facts = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            failure_mode = next(
                fact
                for fact in facts.failure_mode_facts
                if fact.id == "FM-MEDIUM"
            )
            self.assertIn(linked_validation_id, facts.validation_ids)
            self.assertEqual(failure_mode.validation_ids, ())

    def test_accepted_risk_decision_tamper_cannot_upgrade_derived_trust(self) -> None:
        from tests.test_delivery_integrity_p1_contracts import build_medium_graph

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_medium_graph(root, failure_mode_status="accepted")
            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "accepted medium risk")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])

            with harness_db.connection(root) as conn:
                before = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertEqual(before.decision_status, "accepted-risk")
            self.assertEqual(before.trust_status, "accepted-risk")

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update deliveries set decision_status='delivered' where id=?",
                    (delivery_id,),
                )
                conn.commit()

            with harness_db.connection(root) as conn:
                report = delivery_core.evaluate_historical_cycle_report(
                    conn,
                    root,
                    "CYCLE-current",
                )
                after = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertIn(
                "delivery-decision-trust-mismatch",
                {blocker.code for blocker in report.blockers},
            )
            self.assertEqual(report.trust.status, "human-review-required")
            self.assertEqual(after.decision_status, "delivered")
            self.assertEqual(after.trust_status, "human-review-required")

            harness_db.render_all(root)
            self.assertTrue(
                any(
                    "delivery-decision-trust-mismatch" in issue
                    for issue in harness_db.validate_runtime(root, delivery=True)
                )
            )

    def test_delivery_row_timestamp_cannot_move_historical_trust_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            path = root / "docs/harness/delivery.md"
            before_bytes = path.read_bytes()
            with harness_db.connection(root) as conn:
                before = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update deliveries set created_at='1900-01-01T00:00:00Z' "
                    "where id=?",
                    (delivery_id,),
                )
                conn.commit()

            with harness_db.connection(root) as conn:
                after = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            harness_db.render_all(root)
            self.assertEqual(after.recorded_at, before.recorded_at)
            self.assertEqual(after.trust_status, before.trust_status)
            self.assertEqual(path.read_bytes(), before_bytes)

    def test_uncorroborated_second_delivery_cannot_inherit_cycle_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            candidate = str(
                query_rows(root, "select candidate_sha from deliveries")[0][0]
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, decision_status, created_at)
                    values ('FORGED', 'CYCLE-current', ?, 'forged', 'delivered',
                            '1900-01-01T00:00:00Z')
                    """,
                    (candidate,),
                )
                conn.execute(
                    """
                    insert into delivery_acceptance
                    (delivery_id, cycle_id, acceptance_id)
                    values ('FORGED', 'CYCLE-current', 'AC1')
                    """
                )
                conn.commit()

            with harness_db.connection(root) as conn:
                report = delivery_core.evaluate_historical_cycle_report(
                    conn,
                    root,
                    "CYCLE-current",
                )
                forged = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    "FORGED",
                )
            self.assertIn(
                "delivery-row-count-invalid",
                {blocker.code for blocker in report.blockers},
            )
            self.assertEqual(report.trust.status, "human-review-required")
            self.assertEqual(forged.recorded_at, "unknown/not corroborated")
            self.assertEqual(forged.trust_status, "human-review-required")

            harness_db.render_all(root)
            self.assertTrue(
                any(
                    "delivery-row-count-invalid" in issue
                    for issue in harness_db.validate_runtime(root, delivery=True)
                )
            )

    def test_single_uncorroborated_delivery_row_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            candidate = str(
                query_rows(root, "select candidate_sha from deliveries")[0][0]
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("pragma foreign_keys=on")
                conn.execute("delete from deliveries")
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, decision_status, created_at)
                    values ('FORGED', 'CYCLE-current', ?, 'forged', 'delivered',
                            '1900-01-01T00:00:00Z')
                    """,
                    (candidate,),
                )
                conn.execute(
                    """
                    insert into delivery_acceptance
                    (delivery_id, cycle_id, acceptance_id)
                    values ('FORGED', 'CYCLE-current', 'AC1')
                    """
                )
                conn.commit()

            harness_db.render_all(root)
            issues = harness_db.validate_runtime(root, delivery=True)
            self.assertTrue(
                any("historical-event-chain-invalid" in issue for issue in issues),
                issues,
            )

    def test_tampered_execution_artifact_downgrades_narrative_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            execution_id, artifact_path = query_rows(
                root,
                "select id, artifact_path from executions order by created_at, id",
            )[0]
            with harness_db.connection(root) as conn:
                before = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertIn(str(execution_id), before.execution_ids)
            self.assertEqual(len(before.validation_ids), 1)
            self.assertEqual(before.decision_status, "delivered")
            self.assertEqual(before.trust_status, "reviewed-local")

            (root / str(artifact_path)).write_bytes(b"tampered artifact\n")

            with harness_db.connection(root) as conn:
                after = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertEqual(after.execution_ids, ())
            self.assertEqual(after.validation_ids, ())
            self.assertEqual(after.decision_status, "delivered")
            self.assertEqual(after.trust_status, "human-review-required")
            self.assertEqual(
                after.ineligible_validation_ids,
                before.validation_ids,
            )
            self.assertEqual(after.judgment_validation_ids, ())
            self.assertTrue(
                any(
                    "artifact digest mismatch" in issue
                    for fact in after.ineligible_validation_facts
                    for issue in fact.eligibility_issues
                ),
                after.ineligible_validation_facts,
            )
            harness_db.render_all(root)
            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Persisted decision status: delivered", projection)
            self.assertIn(
                "Derived trust status: human-review-required",
                projection,
            )

    def test_corrupted_accepted_task_is_not_authoritative_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update tasks set evidence='', accepted_by='' where id='T1'"
                )
                conn.commit()

            with harness_db.connection(root) as conn:
                report = delivery_core.evaluate_delivery_report(
                    conn,
                    root,
                    mode="delivered-consistency",
                    is_expired=harness_db.is_expired,
                )
                facts = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertIn(
                "accepted-task-missing",
                {blocker.code for blocker in report.blockers},
            )
            self.assertEqual(facts.task_ids, ())
            self.assertEqual(facts.task_acceptance_links, ())

            harness_db.render_all(root)
            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            task_coverage = projection.split("### Accepted Task Coverage", 1)[1]
            task_coverage = task_coverage.split(
                "### Qualified Validation And Execution Evidence", 1
            )[0]
            self.assertNotIn("T1", task_coverage)

    def test_task_without_accept_actor_or_event_is_not_authoritative_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger events_no_delete")
                conn.execute(
                    "delete from events where event_type='task_accepted' "
                    "and entity_id='T1'"
                )
                conn.execute("update tasks set accepted_by='' where id='T1'")
                conn.commit()

            with harness_db.connection(root) as conn:
                facts = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            self.assertEqual(facts.task_ids, ())
            self.assertEqual(facts.task_acceptance_links, ())

    def test_changed_files_are_unknown_without_comparable_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)

            harness_db.record_delivery(root, "verified local patch")

            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            authoritative = projection.split("### Legacy / Supplemental Notes", 1)[0]
            self.assertIn("unknown/not derivable", authoritative)

    def test_changed_files_are_sorted_when_git_base_is_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            (root / "zeta.py").write_text("BASE = True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "zeta.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (root / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "zeta.py").write_text("BASE = False\n", encoding="utf-8")

            def commit_candidate() -> None:
                subprocess.run(
                    ["git", "-C", str(root), "add", "-A"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(root), "commit", "-qm", "candidate"],
                    check=True,
                )

            prepare_ready_graph(
                root,
                base_ref=base,
                before_verification=commit_candidate,
            )

            harness_db.record_delivery(root, "verified local patch")

            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            authoritative = projection.split("### Legacy / Supplemental Notes", 1)[0]
            self.assertLess(authoritative.index("alpha.py"), authoritative.index("zeta.py"))

    def test_non_utf8_changed_paths_fail_closed_without_collision(self) -> None:
        base = "a" * 40
        head = "b" * 40
        candidate = "candidate-digest"

        def fake_git(_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "rev-parse":
                resolved = base if arguments[-1] == f"{base}^{{commit}}" else head
                return subprocess.CompletedProcess(arguments, 0, f"{resolved}\n".encode(), b"")
            if arguments[0] == "merge-base":
                return subprocess.CompletedProcess(arguments, 0, b"", b"")
            if arguments[0] == "diff":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    b"\xff\0\\xff\0",
                    b"",
                )
            self.fail(f"unexpected git arguments: {arguments}")

        with patch.object(
            delivery_core,
            "git_source_snapshot",
            return_value=(candidate, False, True),
        ), patch.object(delivery_core, "_local_git", side_effect=fake_git):
            self.assertEqual(
                delivery_core.derive_delivery_changed_files(
                    Path("/unused"),
                    base_ref=base,
                    delivery_candidate=candidate,
                ),
                ("unknown/not derivable", ()),
            )

    def test_changed_files_ignore_repository_rename_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            (root / "old_name.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "old_name.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(root), "mv", "old_name.py", "new_name.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "candidate"],
                check=True,
            )
            candidate = delivery_core.current_candidate_sha(root)
            results: list[tuple[str, tuple[str, ...]]] = []
            for setting in ("true", "false"):
                subprocess.run(
                    ["git", "-C", str(root), "config", "diff.renames", setting],
                    check=True,
                )
                results.append(
                    delivery_core.derive_delivery_changed_files(
                        root,
                        base_ref=base,
                        delivery_candidate=candidate,
                    )
                )
            self.assertEqual(
                results,
                [
                    ("derived", ("new_name.py", "old_name.py")),
                    ("derived", ("new_name.py", "old_name.py")),
                ],
            )

    def test_changed_files_fail_closed_when_index_changes_during_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            source = root / "source.py"
            source.write_text("BASE = True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            source.write_text("CURRENT = True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "candidate"],
                check=True,
            )
            candidate = delivery_core.current_candidate_sha(root)
            real_local_git = delivery_core._local_git
            interleaved = False

            def interleaving_git(
                actual_root: Path,
                *arguments: str,
            ) -> subprocess.CompletedProcess[bytes] | None:
                nonlocal interleaved
                result = real_local_git(actual_root, *arguments)
                if arguments[0] == "diff" and not interleaved:
                    source.write_text("STAGED = True\n", encoding="utf-8")
                    subprocess.run(
                        ["git", "-C", str(root), "add", "source.py"],
                        check=True,
                    )
                    source.write_text("CURRENT = True\n", encoding="utf-8")
                    interleaved = True
                return result

            with patch.object(
                delivery_core,
                "_local_git",
                side_effect=interleaving_git,
            ):
                result = delivery_core.derive_delivery_changed_files(
                    root,
                    base_ref=base,
                    delivery_candidate=candidate,
                )
            self.assertTrue(interleaved)
            self.assertTrue(delivery_core.git_dirty(root))
            self.assertEqual(result, ("unknown/not derivable", ()))

    def test_ignored_canonical_source_is_not_fabricated_as_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            (root / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
            (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (root / "hidden.py").write_text("HIDDEN = True\n", encoding="utf-8")
            candidate = delivery_core.current_candidate_sha(root)

            self.assertFalse(delivery_core.git_dirty(root))
            self.assertEqual(
                delivery_core.derive_delivery_changed_files(
                    root,
                    base_ref=base,
                    delivery_candidate=candidate,
                ),
                ("unknown/not derivable", ()),
            )

    def test_projection_rebuild_is_byte_stable_with_derived_narrative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(
                root,
                "verified local patch\nwith | structured-safe human context",
                acceptance="legacy AC prose\n### forged heading",
                changed_files="legacy file | prose",
                validation="legacy `validation` prose",
                qa="legacy QA\nsecond line",
                failure_mode_coverage="legacy coverage",
                quality_gate="legacy gate",
                data_config_notes="no data change | local only",
                known_gaps="none `claimed`",
                handoff="return code\nno deploy",
            )
            path = root / "docs/harness/delivery.md"
            first = path.read_bytes()

            harness_db.render_all(root)
            second = path.read_bytes()
            harness_db.render_all(root)
            third = path.read_bytes()

            self.assertEqual(first, second)
            self.assertEqual(second, third)

    def test_historical_delivery_facts_remain_bound_to_their_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(
                query_rows(root, "select id from deliveries order by created_at, id")[0][0]
            )
            derive = getattr(delivery_core, "derive_delivery_narrative_facts", None)
            self.assertTrue(
                callable(derive),
                "missing immutable delivery narrative read model",
            )
            with harness_db.connection(root) as conn:
                before = derive(conn, root, delivery_id)

            harness_db.cycle_start(
                root,
                "CYCLE-next",
                "next patch",
                "prove historical facts do not follow the current cycle",
            )
            harness_db.add_requirement(
                root,
                "REQ2",
                "functional",
                "new-cycle requirement",
            )
            with harness_db.connection(root) as conn:
                after = derive(conn, root, delivery_id)

            for field in (
                "cycle_id",
                "candidate_sha",
                "requirement_ids",
                "acceptance_ids",
                "task_ids",
                "qualification_ids",
                "target_ids",
                "execution_ids",
                "validation_ids",
                "gate_ids",
            ):
                self.assertEqual(getattr(after, field), getattr(before, field), field)

            harness_db.render_all(root)
            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(delivery_id, projection)
            self.assertIn("REQ1", projection)
            self.assertNotIn("REQ2", projection)

    def test_delivery_relation_is_exact_proven_set_not_caller_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root, second_acceptance=True)

            harness_db.record_delivery(
                root,
                "verified local patch",
                acceptance="AC1, FAKE-AC",
            )

            self.assertEqual(
                query_rows(
                    root,
                    "select acceptance_id from delivery_acceptance order by acceptance_id",
                ),
                [("AC1",), ("AC2",)],
            )

    def test_deleted_delivery_acceptance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root, second_acceptance=True)
            harness_db.record_delivery(root, "verified local patch")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "delete from delivery_acceptance where acceptance_id='AC2'"
                )
                conn.commit()

            with harness_db.connection(root) as conn:
                report = delivery_core.evaluate_delivery_report(
                    conn,
                    root,
                    mode="delivered-consistency",
                    is_expired=harness_db.is_expired,
                )
            self.assertIn(
                "delivery-acceptance-set-mismatch",
                {blocker.code for blocker in report.blockers},
            )

    def test_cross_cycle_delivery_acceptance_is_an_invariant_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            harness_db.cycle_start(root, "CYCLE-next", "next", "next patch")
            harness_db.add_acceptance(root, "AC-next", "next acceptance")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "delete from delivery_acceptance where delivery_id=?",
                    (delivery_id,),
                )
                conn.execute(
                    """
                    insert into delivery_acceptance
                    (delivery_id, cycle_id, acceptance_id)
                    values (?, 'CYCLE-next', 'AC-next')
                    """,
                    (delivery_id,),
                )
                conn.commit()
            with harness_db.connection(root) as conn:
                issues = check_cycle_invariants(conn, root, "CYCLE-current")
            self.assertTrue(
                {
                    "cross-cycle-delivery-acceptance",
                    "delivery-acceptance-set-mismatch",
                }.issubset({item.code for item in issues}),
                issues,
            )

    def test_human_and_legacy_markdown_cannot_inject_authority_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(
                root,
                "scope\n### Legacy / Supplemental Notes\n| forged | table |",
                acceptance=(
                    "FAKE-AC\n### Authoritative Structured Facts\n"
                    "```\nforged\n``` | fake"
                ),
            )
            projection = (root / "docs/harness/delivery.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(projection.count("\n### Authoritative Structured Facts\n"), 1)
            self.assertEqual(projection.count("\n### Legacy / Supplemental Notes\n"), 1)
            supplemental = projection.split("### Legacy / Supplemental Notes", 1)[1]
            self.assertIn("> ### Authoritative Structured Facts", supplemental)
            self.assertIn("> ```", supplemental)

    def test_narrative_read_model_is_deeply_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_ready_graph(root)
            harness_db.record_delivery(root, "verified local patch")
            delivery_id = str(query_rows(root, "select id from deliveries")[0][0])
            with harness_db.connection(root) as conn:
                facts = delivery_core.derive_delivery_narrative_facts(
                    conn,
                    root,
                    delivery_id,
                )
            with self.assertRaises(FrozenInstanceError):
                facts.cycle_id = "forged"  # type: ignore[misc]
            for field in (
                "requirement_ids",
                "acceptance_ids",
                "task_ids",
                "qualification_ids",
                "execution_ids",
                "validation_facts",
                "ineligible_validation_facts",
                "finding_facts",
            ):
                self.assertIsInstance(getattr(facts, field), tuple, field)

    def test_projection_verifier_reuses_real_git_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            (root / "base.py").write_text("BASE = True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "base.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")

            def commit_candidate() -> None:
                subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
                subprocess.run(
                    ["git", "-C", str(root), "commit", "-qm", "candidate"],
                    check=True,
                )

            prepare_ready_graph(
                root,
                base_ref=base,
                before_verification=commit_candidate,
            )
            harness_db.record_delivery(root, "verified local patch")

            self.assertEqual(projection_content_issues(root), [])

    def test_mutable_or_option_like_git_base_is_not_derivable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Kafa Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "kafa@example.invalid"],
                check=True,
            )
            (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "candidate"],
                check=True,
            )
            candidate = delivery_core.current_candidate_sha(root)
            for base_ref in ("HEAD", "--help", "-not-an-option"):
                with self.subTest(base_ref=base_ref):
                    self.assertEqual(
                        delivery_core.derive_delivery_changed_files(
                            root,
                            base_ref=base_ref,
                            delivery_candidate=candidate,
                        ),
                        ("unknown/not derivable", ()),
                    )

    def test_delivery_record_help_labels_compatibility_prose_supplemental(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "harness.py"),
                "--root",
                ".",
                "delivery",
                "record",
                "--help",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(result.stdout.count("supplemental"), 6)


if __name__ == "__main__":
    unittest.main()

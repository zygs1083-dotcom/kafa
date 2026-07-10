from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def create_schema28_fixture(root: Path) -> None:
    path = db_path(root)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            pragma foreign_keys = on;
            create table project (
                id integer primary key check (id = 1), project_id text not null,
                schema_version integer not null, runtime_version text not null,
                phase text not null, current_cycle_id text not null,
                connector_project_key text not null default '', status text not null,
                scope_status text not null, current_owner text not null,
                revision integer not null, updated_at text not null
            );
            create table delivery_cycles (
                id text primary key, name text not null, goal text not null,
                status text not null, phase text not null, base_ref text not null default '',
                candidate_sha text not null default '', started_at text not null,
                closed_at text not null default '', created_at text not null, updated_at text not null
            );
            create table requirements (
                id text primary key, cycle_id text not null, kind text not null, body text not null,
                priority text not null default '', status text not null default 'active',
                tool_link text not null default '', revision integer not null default 1, updated_at text not null
            );
            create table acceptance (
                id text primary key, cycle_id text not null, criterion text not null,
                priority text not null default '', tool_link text not null default '',
                status text not null default 'active', revision integer not null default 1
            );
            create table failure_modes (
                id text primary key, cycle_id text not null, feature text not null,
                scenario text not null, trigger text not null, expected_behavior text not null,
                recovery text not null default '', data_safety text not null default '',
                risk text not null, status text not null, accepted_by text,
                acceptance_reason text, acceptance_scope text not null default '',
                accepted_revision integer, expires_at text, revision integer not null default 1
            );
            create table tasks (
                id text primary key, cycle_id text not null, task text not null, owner text not null,
                status text not null, evidence text not null default '', tool_link text not null default '',
                submitted_by text not null default '', submitted_session_id text not null default '',
                accepted_by text not null default '', accepted_session_id text not null default '',
                lease_agent text, lease_token text, lease_heartbeat_at text, lease_expires_at text,
                retry_count integer not null default 0, retry_budget integer not null default 2,
                fence integer not null default 0, revision integer not null default 1, updated_at text not null
            );
            create table requirement_acceptance (
                requirement_id text not null references requirements(id),
                acceptance_id text not null references acceptance(id),
                primary key (requirement_id, acceptance_id)
            );
            create table failure_mode_acceptance (
                failure_mode_id text not null references failure_modes(id),
                acceptance_id text not null references acceptance(id),
                primary key (failure_mode_id, acceptance_id)
            );
            create table task_acceptance (
                task_id text not null references tasks(id), acceptance_id text not null references acceptance(id),
                primary key (task_id, acceptance_id)
            );
            create table task_failure_modes (
                task_id text not null references tasks(id), failure_mode_id text not null references failure_modes(id),
                primary key (task_id, failure_mode_id)
            );
            create table quality_gates (
                id text primary key, cycle_id text not null, candidate_sha text not null,
                gate text not null, reviewed_commit text not null, evidence_commit text not null default '',
                diff_hash text not null default '', base_commit text not null default '',
                head_commit text not null default '', tracked_diff_hash text not null default '',
                project_revision integer not null default 0, reviewer_context text not null,
                result text not null, blocking_findings text not null default '', commands text not null default '',
                evidence text not null default '', residual_risk text not null default '',
                reviewer_session_id text not null default '', reviewer_attestation_id text not null default '',
                review_trust_level text not null default 'local-only', created_at text not null
            );
            create table agent_sessions (
                session_id text primary key, agent_id text not null, role text not null,
                context_id text not null, provider_session_id text not null default '',
                origin text not null, trust_level text not null, status text not null,
                started_at text not null, ended_at text not null default ''
            );
            create table session_attestations (
                id text primary key, session_id text not null, agent_id text not null, role text not null,
                context_id text not null, provider_session_id text not null default '', origin text not null,
                verification_token text not null, token_status text not null, token_reason text not null default '',
                trust_level text not null, created_at text not null
            );
            create table ci_verifications (
                id text primary key, provider text not null, run_id text not null, conclusion text not null,
                commit_sha text not null, origin text not null, verification_token text not null,
                token_status text not null, token_reason text not null default '', external_link text not null default '',
                created_at text not null, unique(provider, run_id)
            );
            create table external_session_verifications (
                id text primary key, session_id text not null, verifier text not null, conclusion text not null,
                commit_sha text not null, origin text not null, verification_token text not null,
                token_status text not null, token_reason text not null default '', external_link text not null default '',
                created_at text not null, unique(session_id, verifier)
            );
            create table migrations (
                id integer primary key autoincrement, from_version integer not null,
                to_version integer not null, applied_at text not null
            );
            create table events (
                sequence integer primary key autoincrement, id text not null unique,
                schema_version integer not null, type text not null, source text not null,
                target text not null, correlation_id text not null default '',
                causation_id text not null default '', idempotency_key text not null default '',
                payload_json text not null, created_at text not null
            );
            """
        )
        conn.executescript(
            """
            insert into project values
              (1, 'project-1', 28, '4.18.0', 'qa', 'CYCLE-current', 'project',
               'active', 'confirmed', 'project-manager', 7, '2026-07-10T00:00:00Z');
            insert into delivery_cycles values
              ('CYCLE-current', 'Current', 'Ship safely', 'active', 'qa', '', 'candidate-1',
               '2026-07-10T00:00:00Z', '', '2026-07-10T00:00:00Z', '2026-07-10T00:00:00Z');
            insert into requirements values
              ('R1', 'CYCLE-current', 'functional', 'Preserve requirement', 'must', 'active', '', 1, '2026-07-10T00:00:00Z');
            insert into acceptance values
              ('AC1', 'CYCLE-current', 'Preserve acceptance', 'must', '', 'active', 1);
            insert into failure_modes values
              ('FM1', 'CYCLE-current', 'Delivery', 'Failure', 'trigger', 'safe', '', '',
               'critical', 'identified', null, null, '', null, null, 1);
            insert into tasks values
              ('T1', 'CYCLE-current', 'Preserve task', 'developer', 'ready', '', '', '', '', '', '',
               null, null, null, null, 0, 2, 0, 1, '2026-07-10T00:00:00Z');
            insert into requirement_acceptance values ('R1', 'AC1');
            insert into failure_mode_acceptance values ('FM1', 'AC1');
            insert into task_acceptance values ('T1', 'AC1');
            insert into task_failure_modes values ('T1', 'FM1');
            insert into quality_gates values
              ('z-old-pass', 'CYCLE-current', 'candidate-1', 'independent_qa', 'HEAD', '', '', '', '', '',
               7, 'fresh', 'pass', '', 'test', 'EV1', '', 'S-review', 'ATT1', 'connector', '2026-07-10T00:00:00Z');
            insert into quality_gates values
              ('a-new-fail', 'CYCLE-current', 'candidate-1', 'independent_qa', 'HEAD', '', '', '', '', '',
               7, 'fresh', 'fail', '', 'test', 'EV2', '', 'S-review', 'ATT1', 'connector', '2026-07-10T00:00:00Z');
            insert into agent_sessions values
              ('S-review', 'qa-reviewer', 'qa-reviewer', 'ctx', '', 'connector', 'connector', 'active',
               '2026-07-10T00:00:00Z', '');
            insert into session_attestations values
              ('ATT1', 'S-review', 'qa-reviewer', 'qa-reviewer', 'ctx', '', 'connector', 'self-signed',
               'hmac-valid', 'legacy', 'connector', '2026-07-10T00:00:00Z');
            insert into ci_verifications values
              ('github:1', 'github', '1', 'success', 'abc', 'connector', 'self-signed',
               'hmac-valid', 'legacy', '', '2026-07-10T00:00:00Z');
            insert into external_session_verifications values
              ('external:1', 'S-review', 'host', 'pass', 'abc', 'connector', 'self-signed',
               'hmac-valid', 'legacy', '', '2026-07-10T00:00:00Z');
            """
        )
        conn.commit()


class Schema29MigrationTest(unittest.TestCase):
    def test_fresh_schema_supports_cycle_local_ids_with_internal_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(
                run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "First").returncode,
                0,
            )
            self.assertEqual(run_harness(root, "cycle", "close", "--status", "archived").returncode, 0)
            self.assertEqual(
                run_harness(root, "cycle", "start", "--id", "CYCLE-next", "--name", "Next", "--goal", "Iterate").returncode,
                0,
            )
            second = run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Second")
            with closing(sqlite3.connect(db_path(root))) as conn:
                version = conn.execute("select schema_version from project where id = 1").fetchone()[0]
                columns = {row[1] for row in conn.execute("pragma table_info(requirements)")}
                rows = (
                    conn.execute("select uid, cycle_id, id, body from requirements order by cycle_id").fetchall()
                    if "uid" in columns
                    else []
                )

        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(version, 29)
        self.assertIn("uid", columns)
        self.assertEqual([(row[1], row[2], row[3]) for row in rows], [
            ("CYCLE-current", "R1", "First"),
            ("CYCLE-next", "R1", "Second"),
        ])
        self.assertEqual(len({row[0] for row in rows}), 2)
        self.assertTrue(all(row[0] for row in rows))

    def test_schema28_to_29_preserves_links_and_downgrades_unprovable_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema28_fixture(root)

            result = run_harness(root, "migrate", "--from-version", "28", "--to-version", "29")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                project = conn.execute("select schema_version from project where id = 1").fetchone()
                facts = {
                    table: conn.execute(f"select uid, cycle_id, id from {table}").fetchall()
                    for table in ["requirements", "acceptance", "failure_modes", "tasks"]
                }
                links = {
                    table: conn.execute(f"select count(*) as count from {table}").fetchone()["count"]
                    for table in ["requirement_acceptance", "failure_mode_acceptance", "task_acceptance", "task_failure_modes"]
                }
                gates = conn.execute(
                    "select id, sequence, gate_status, superseded_by, review_trust_level from quality_gates order by sequence"
                ).fetchall()
                attestation = conn.execute(
                    "select trust_level, effective_trust, receipt_provenance from session_attestations where id = 'ATT1'"
                ).fetchone()
                agent_session = conn.execute(
                    "select trust_level, effective_trust from agent_sessions where session_id = 'S-review'"
                ).fetchone()
                ci = conn.execute(
                    "select effective_trust, receipt_provenance from ci_verifications where id = 'github:1'"
                ).fetchone()
                external = conn.execute(
                    "select effective_trust, receipt_provenance from external_session_verifications where id = 'external:1'"
                ).fetchone()
                foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(project["schema_version"], 29)
        self.assertTrue(all(len(rows) == 1 and rows[0]["uid"] for rows in facts.values()))
        self.assertEqual(links, {
            "requirement_acceptance": 1,
            "failure_mode_acceptance": 1,
            "task_acceptance": 1,
            "task_failure_modes": 1,
        })
        self.assertEqual({row["gate_status"] for row in gates}, {"legacy-ambiguous"})
        self.assertEqual(len({row["sequence"] for row in gates}), 2)
        self.assertTrue(all(row["review_trust_level"] == "legacy-untrusted" for row in gates))
        self.assertEqual(tuple(attestation), ("connector", "legacy-untrusted", "schema28-unprovable"))
        self.assertEqual(tuple(agent_session), ("connector", "legacy-untrusted"))
        self.assertEqual(tuple(ci), ("legacy-untrusted", "schema28-unprovable"))
        self.assertEqual(tuple(external), ("legacy-untrusted", "schema28-unprovable"))
        self.assertEqual(foreign_key_errors, [])


if __name__ == "__main__":
    unittest.main()

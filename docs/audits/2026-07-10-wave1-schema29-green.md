# Wave 1 Schema 29 Verification

Date: 2026-07-10

Branch: `v1.26-stop-ship-correctness`

## Scope

This batch establishes the schema 29 storage contract required by CY-001 and DT-002 without relaxing delivery or trust gates.

- cycle-owned facts use immutable `uid` values and `(cycle_id, id)` public identity;
- relation rows carry `cycle_id` and enforce same-cycle composite foreign keys;
- quality gates use transactionally allocated `sequence` values and explicit supersession;
- schema 28 connector trust is preserved for audit but downgraded to `legacy-untrusted`;
- schema 24 legacy rows move to `CYCLE-legacy`, while schema 25+ cycle ownership is preserved;
- checkpoints from schema 28 are rejected before restore until an explicit conversion exists;
- every state-changing transaction records canonical row mutations; replay restores cycle/project changes, relations, supersession side effects, and cycle-owned facts;
- schema 29 events without a mutation journal and replay across a schema migration boundary fail closed;
- current working projections, baseline, scheduler, dispatch, and traceability are cycle-scoped.

## Failure Evidence Closed

- CY-001: two cycles can reuse `R1`, `AC1`, `FM1`, and `T1` without moving or mutating the earlier cycle. Claiming the current `T1` leaves the archived `T1` unchanged.
- DT-002: a newer gate receives a larger database sequence, supersedes the previous active gate in the same transaction, and is the only gate eligible for readiness.
- Schema lifecycle: schema 28 rebuild preserves fact/link counts and passes `pragma foreign_key_check`.
- Migration rollback: injected post-migration schema failure leaves schema version 28, old table shape, rows, and migration history unchanged.
- Replay: a checkpoint plus later events reconstruct current cycle/project state, relations, two cycle-local `R1` rows, and quality-gate supersession.
- Scoped invariants and expired-lease repair resolve cycle-owned facts by current cycle or immutable `uid`, never by a bare local ID.
- Store seam: database backup uses the Store API and SQLite backup primitive without leaking connections.

## Verification

Targeted checks:

```text
Targeted schema lifecycle, migration, cycle, replay, dispatch, and provider tests: PASS
plugin structure validation: PASS
git diff --check: PASS
```

Full regression:

```text
Ran 274 tests in 458.741s
Expected failing stop-ship regressions: 4
Unexpected failures: 0
```

The four remaining failures are intentionally retained and were not weakened:

- DT-001: open critical structured findings do not yet block delivery;
- TR-001: the ordinary CLI can still self-issue connector trust;
- QS-001: quickstart still manufactures QA/gate/delivery state;
- IN-001: user-scope marketplace source resolution is still invalid.

## Release State

Stop-ship remains active. This batch is not release evidence and does not authorize push, merge, or publication.

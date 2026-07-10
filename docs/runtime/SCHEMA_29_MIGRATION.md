# Schema 29 Migration Contract

## Purpose

Schema 29 provides the storage foundation for deterministic delivery truth. It does not relax any delivery or trust gate.

## Cycle-owned identity

`requirements`, `acceptance`, `failure_modes`, and `tasks` use:

- `uid`: immutable internal primary key, never supplied as the user-facing CLI ID;
- `id`: cycle-local public ID such as `R1`, `AC1`, `FM1`, or `T1`;
- `cycle_id`: owning delivery cycle;
- `unique(cycle_id, id)`: public identity boundary.

Legacy schema 28 rows migrate with a new generated `uid`, while preserving the old `id` as the local ID. Relation tables retain their public ID columns, add `cycle_id`, and use composite foreign keys to enforce same-cycle links.

Covered relation tables:

- `requirement_acceptance`;
- `failure_mode_acceptance`;
- `task_acceptance`;
- `task_failure_modes`;
- `task_dependencies`;
- `task_test_targets`;
- `validation_failure_modes`;
- `delivery_acceptance`;
- `task_attempts` and `dispatch_assignments` where a direct task foreign key exists.

Generic audit references such as invalidation source/target IDs retain local IDs but are always interpreted with their stored `cycle_id`.

## Quality gate ordering

`quality_gates` adds:

- `sequence`: database-assigned monotonic order;
- `gate_status`: `active`, `superseded`, or `legacy-ambiguous`;
- `superseded_by`: ID of the newer gate.

New gates for the same `(cycle_id, candidate_sha)` supersede the previous active gate in the same transaction. Schema 28 gates with distinct timestamps are ordered by timestamp and insertion order. Gates sharing the same schema 28 timestamp cannot prove their historical order and migrate as `legacy-ambiguous`; they cannot satisfy delivery.

## Legacy trust downgrade

Schema 28 cannot prove whether a connector HMAC was externally issued or locally self-signed. Migration preserves original origin/token fields for audit and adds:

- `effective_trust`;
- `receipt_provenance`.

Every pre-schema-29 connector-trusted session attestation, CI verification, external-session verification, agent session, and cached quality-gate reviewer trust is downgraded to `legacy-untrusted`. No migration heuristic may retain connector delivery eligibility.

## Transaction and recovery

- Schema 28 to 29 is one `BEGIN IMMEDIATE` transaction.
- Child relations are rebuilt before parent tables are replaced.
- Row counts, link counts, `foreign_key_check`, and full runtime invariants must pass before commit.
- Failure leaves schema 28 and all business rows unchanged.
- Dry-run validates the registered path without mutating the database.
- Backups remain recovery artifacts; transaction losers never restore a stale backup over a winner.

## Compatibility boundary

- Public CLI IDs remain unchanged.
- Checkpoint packages remain schema-versioned; schema 28 packages require an explicit in-memory conversion before schema 29 restore.
- Events remain immutable audit records. New events include cycle-local identity context; old event payloads are not rewritten.

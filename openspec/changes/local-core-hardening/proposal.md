## Why

The completed `local-core-slimming` candidate passes its existing regression
matrix, but an independent adversarial audit found three uncovered failure
paths. Ordinary SQLite operations do not participate in the migration lock,
generated projections can remain on schema 30 after the database rolls back,
and a `same-context-degraded` gate can supply distinct-looking context IDs to
the high-risk accepted-risk path. These gaps can cause data loss, split-brain
operator views, or an internally inconsistent high-risk decision.

This follow-up change fixes those defects without changing schema 30, adding a
second lifecycle, or weakening the local-only trust boundary.

## What Changes

- Coordinate every file-backed runtime database operation and schema migration
  with one cross-platform, same-thread-reentrant project operation lock.
- Preserve the existing migration sentinel for diagnostics, and make new
  operations fail closed once migration begins while allowing an already active
  operation to finish before backup and staging start.
- Back up the exact generated projection files beside the SQLite backup and
  restore both authorities coherently after any post-activation failure.
- Require `review_status=reviewed-local` for high/critical accepted-risk
  decisions; distinct-looking IDs cannot promote a degraded review.
- Add deterministic concurrency and rollback tests, then rerun full local,
  live-host, performance, installation, and three-platform workflow gates.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `local-delivery-kernel`: strengthens migration exclusion, rollback coherence,
  and high-risk independent-review metadata.

## Impact

- Affected runtime modules are the Store seam, local-core migration,
  projections, delivery evaluation, and the root CLI integration layer.
- Schema remains 30 with the same 27 tables. Public CLI, Skills, Hooks, agent
  templates, and Native Host ownership do not expand.
- Migration manifests gain bounded derived-view backup and restore metadata.
- Existing user/global Kafa installation is not changed. Remote CI requires
  separate commit/push authorization and remains `not-run` without it.

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
- Persist recovery-required sentinel metadata before activation so hard process
  exit cannot make a split DB/projection authority look like a removable stale
  lock; require projection activation validation for every successful caller.
- Harden the production delivery candidate identity with the same isolated Git,
  ignored-runtime-source, mode, symlink, framing, and local-object rules used by
  Native evaluation evidence.
- Keep ignored runtime source inside the candidate while excluding bounded,
  non-versioned top-level dependency/tool environment roots and generated tool
  caches so documented `.venv` and `node_modules` layouts remain usable.
- Recompute every required controller/task/execution/validation/Host-surface
  field before a Native single or parallel report can remain passing.
- Bind every report to an exact profile mode, scenario inventory, evidence
  scope, current Native binary, positive finite telemetry, and immutable
  parallel task-to-scope contract.
- Version and close the Native report schema, reject scoped unmerged Git source,
  and refuse persistent passing evidence produced through an explicit test
  binary override.
- Validate schema 27/28 project and quality-gate trust revisions before the
  isolated legacy runtime can apply SQLite arithmetic.
- Bind Native matrix facts to the generation environment and make every closed
  passing-report integer recursively type-exact, so JSON booleans or floats
  cannot impersonate counts or return codes.
- Exclude only exact Kafa-generated projection/template paths from candidate
  source; `.gitignore`, adjacent runtime files, and no-Git non-regular paths
  can no longer disappear behind a broad prefix.
- Reject in-scope gitlinks and other non-regular entries whether they exist in
  the index, HEAD, or both; keep generated structured results in Kafa-owned
  runtime state when verification must preserve the candidate.
- Hold the operation lock across public projection rebuild, same-schema migrate,
  repair, and every other production projection publication; independently
  render and compare all 13 live views before migration success.
- Make `project-state.yaml` deterministic from SQLite `project.updated_at` and
  replace rather than merge it so wall-clock changes or stale ad-hoc keys cannot
  alter an unchanged database projection.
- Treat the caller's projection callback as publication only: core migration
  independently verifies every live byte before success, and lock open/release
  cleanup remains safe across `BaseException` cancellation.
- Disable repository replace-object refs for production and Native source
  identity so a replacement commit/tree/blob cannot hide a gitlink or missing
  local object.
- Pin the evaluated Git root through controlled `GIT_WORK_TREE`, reject every
  unexpected catalog table beyond the 27 schema-30 tables plus
  `sqlite_sequence`, and run Native controller commands from a start-verified
  private Git-backed snapshot.
- Require the exact schema-30 active table inventory in passing Native profiles
  and reject every negative evaluator counter before aggregation.
- Treat failed schema-30 WAL/SHM as part of rollback authority: quarantine and
  verify sidecars, use ordinary SQLite restore validation, and retain recovery
  state whenever handles or sidecars cannot be neutralized.
- Check recovery sentinels before missing-DB initialization guidance, and clear
  a handled pre-activation sentinel only after the unchanged DB and exact
  projection backup have been explicitly verified.
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

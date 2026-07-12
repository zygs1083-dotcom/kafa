## Context

`local-core-slimming` established a root-controller-only local SQLite kernel,
side-by-side schema-30 migration, generated Markdown projections, and
procedural high-risk acceptance. The existing migration sentinel is only
consulted by migration itself. Normal `SqliteStore` connections can therefore
commit after the last source fingerprint and before `os.replace`. Post-
activation validation currently renders projections before doctor and restores
only SQLite on failure. High-risk evaluation receives context IDs but not the
quality gate's explicit review status.

## Goals

- Prevent every active or new file-backed DB handle from racing migration.
- Include already-started committed work in the migration backup, and reject
  operations that start after migration is announced.
- Restore SQLite and all generated local views to one coherent pre-migration
  state after a failed activation.
- Make degraded review categorically insufficient for high/critical delivery,
  even when all risks are explicitly accepted.
- Preserve schema 30, local-only runtime, performance budgets, and Native Host
  lifecycle ownership.

## Non-Goals

- Authenticating the human/root controller or making local files tamper-proof.
- Adding distributed leases, worker writers, event replay, or a new table.
- Replacing the active user-scoped `1.25.0-beta.1` installation.
- Commit, push, tag, release, or deployment without separate authorization.

## Decision 1: One reentrant cross-platform operation lock

The stable coordination file is
`.ai-team/state/harness.db.operation.lock`. POSIX uses `fcntl.flock`; Windows
uses a one-byte `msvcrt.locking` lock. Acquisition is exclusive, bounded by the
existing five-second database timeout, and same-thread reentrant. A process-
local `threading.RLock` serializes sibling threads; only the thread that owns
the OS lock may reenter it.

Every file-backed `connection`, `transaction`, and backup operation holds the
operation lock until its SQLite handle closes. `InMemoryStore` is unchanged.
This intentionally serializes local readers as well as writers because Windows
cannot safely replace a database with another process holding it open.

Migration follows this order:

1. Atomically create `local-core-migration.lock` with PID and timestamp.
2. Acquire the operation lock, waiting for any operation that began earlier.
3. Re-read source version and fingerprint, then create backup and staging DB.
4. Hold the lock through activation, validation, success, or complete rollback.
5. Remove the sentinel only after the operation lock is released.

Normal operations check the sentinel before acquiring the operation lock and
again after acquisition. A new operation fails with `migration-in-progress`;
an operation already holding the lock completes before migration reads its
source. Migration callbacks in the owning thread reuse the lock. A stale
sentinel is never silently deleted and produces an actionable fail-closed
diagnostic.

## Decision 2: Migration rollback bundle includes projections

`core.projections` exposes one ordered `PROJECTION_PATHS` tuple containing the
project-state YAML and the twelve Markdown view paths written by the thirteen
projection renderers. A separate `PROJECTION_ROLLBACK_PATHS` adds the retired
`docs/harness/evidence.md` path that `render_executions` may delete, so rollback
covers all fourteen filesystem side effects without calling it a live
projection. Before activation, migration copies each existing path
into `<backup-dir>/projections/`, recording relative path, prior existence,
mode, and SHA-256 in `projection_backup`. Missing paths receive an explicit
`existed=false` entry.

Post-activation validation runs database doctor before mutating live views,
then renders and validates all projections. If any post-activation step fails,
the migration moves the failed schema-30 DB aside, restores the verified SQLite
backup, and atomically restores every recorded projection. Files absent before
migration but created during the failed attempt are removed. Restored bytes are
re-hashed before rollback is reported complete.

The manifest field `projection_restore_status` is one of `not-needed`,
`restored`, or `failed`. Any projection restore failure changes the migration
status to `rollback-incomplete`, preserves both exception messages and all
artifact paths, and never reports success.

## Decision 3: Review status is a required trust input

`evaluate_local_trust` gains a required keyword-only `review_status`. For high
or critical risk, the accepted-risk path requires all of:

- a structured current-candidate controller execution;
- `review_status == reviewed-local`;
- non-empty and unequal producer/reviewer context metadata;
- complete, current, unexpired accepted/exempt records for every remaining
  high/critical risk.

Risk acceptance applies only to the named risks; it cannot waive missing
independent review. Low/medium behavior remains unchanged, including explicit
`same-context-degraded` delivery labeling.

## Failure Handling

- Lock timeout or migration sentinel: fail before opening the active DB.
- Process death: OS lock releases automatically; a stale diagnostic sentinel
  remains fail-closed and names the inspection/removal path.
- DB restore succeeds but projection restore fails: keep the restored DB,
  report `rollback-incomplete`, and preserve the projection backup.
- Remote CI, live Host, or platform capability unavailable: record `not-run` or
  `blocked`; fixture evidence never substitutes for it.

## Verification Strategy

Use multiprocessing Events/Pipes instead of timing sleeps. First capture red
failures for the writer/migration race, projection rollback, and degraded high-
risk review. Run targeted suites after each decision, then the complete
ResourceWarning-as-error suite, fixture/stability/live profiles, benchmark,
isolated artifact install, structure/OpenSpec validation, and adversarial QA.
Remote Ubuntu/macOS/Windows CI is authorization-gated and cannot be described
as passed until a pushed revision completes all matrix jobs.

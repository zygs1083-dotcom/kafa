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
3. Re-read source version and fingerprint. For schema 27/28, validate the
   original SQLite storage type and positive value of `project.revision` and
   every `quality_gates.project_revision` before any legacy conversion can run.
4. Create backup and staging DB.
5. Before atomic activation, atomically persist and fsync `recovery-required`
   sentinel metadata including the migration manifest path.
6. Hold the lock through activation, validation, success, or complete rollback.
7. Remove the sentinel only after success or a verified complete rollback;
   retain it across hard process exit, rollback-incomplete, or interrupted recovery.

Normal operations check the sentinel before acquiring the operation lock and
again after acquisition. A new operation fails with `migration-in-progress`;
an operation already holding the lock completes before migration reads its
source. Migration callbacks in the owning thread reuse the lock. A stale
sentinel is never silently deleted and produces an actionable fail-closed
diagnostic.

Production projection publication is one normal operation, not a sequence of
independent DB reads followed by unlocked filesystem writes. The central
`render_all` and `render_affected` wrappers hold the same reentrant operation
lock across every selected renderer. This includes public `projection rebuild`,
same-schema migrate, repair, ordinary mutation projections, and the active
migration callback. In-memory test stores keep their lock-free seam.

`project-state.yaml` is a pure projection of the locked SQLite authority, not
of the renderer's wall clock or the previous generated file. Its timestamp is
the persisted `project.updated_at`; publication must replace rather than merge
the generated state so unchanged database facts render byte-identically and a
rebuild removes stale ad-hoc keys.
Its key set is exactly the project-state schema: DB `id` and `current_cycle_id`
are present, while generic writer-only `blocked_reason` is not fabricated.

Lock acquisition and release cleanup catches the full `BaseException` family
for resource cleanup while preserving the original cancellation. A cancellation
during descriptor initialization or OS unlock must still close the descriptor,
release the process-local lock, and leave the next operation able to acquire the
same project lock.

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

The core migration entrypoint requires an explicit projection activation
validator. A caller that omits it is rejected before activation. The public CLI
validator owns the mandatory render-and-verify step; test-only failure
injection may provide a bounded validator but cannot turn a no-validator run
into an activated success.

Existence is not projection verification. After live publication, doctor backs
up the active database to a private temporary root, renders all 13 expected
views there, and compares every ordered live path byte-for-byte. A silent no-op
or corrupt renderer therefore triggers activation rollback.

The core entrypoint performs that independent comparison after the caller's
publication callback returns. Callback self-report is never the success trust
boundary: a non-null no-op callback still causes rollback before the manifest
can become `activated` or the recovery sentinel can be cleared.
Before publication, core establishes stable WAL mode and fingerprints the active
database; any callback-era database authority change fails even when the new
fact is schema-valid and matching views were rendered. Core then reruns schema
doctor before the independent byte comparison.

Failed schema-30 WAL and SHM files belong to the failed authority. Rollback
quarantines them beside the failed schema-30 main database before restoring the
verified source backup. The restored database is opened with ordinary read-only
SQLite semantics, not `immutable=1`, so stale sidecar replay cannot be hidden.
If a platform handle prevents sidecar quarantine or ordinary validation,
rollback remains incomplete and the recovery sentinel is retained.

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

## Decision 4: Production candidate identity uses hardened runtime bytes

The real delivery gate and the Native report generator share the same isolated
Git environment, local-object check, and fixed path/mode/SHA-256 framing
primitives. Candidate enumeration includes ignored runtime source because Git
ignore status is not proof that a loader cannot execute it. Kafa-owned
`.ai-team/`, generated agent templates, generated harness views, `.git/`, and
generated Python/tool caches remain excluded so Kafa's own evidence writes do
not make the candidate stale.

Generated exclusions are exact outside the reserved `.ai-team/` state root:
only the three installed Native agent templates and the six exact generated projection
or retired-projection paths are omitted. `.gitignore`, any extra
`.codex/agents/` file, and any extra `docs/harness/` file remain candidate
source. No-Git identity fails closed on FIFO, socket, device, and every other
non-regular path just as Git identity does.

The Native evaluation source identity follows the same scoped unmerged rule as
production identity: any unmerged `kafa/`, `plugins/`, `tests/`, `benchmarks/`,
or named evaluation file invalidates the report source instead of producing a
dirty but still usable digest.

An exact, bounded set of non-versioned top-level dependency/tool environment
roots is also outside source identity: `.venv/`, `venv/`, `.tox/`, `.nox/`,
and `node_modules/`. These are installed execution environments rather than
project source; hashing them makes the documented `.venv` workflow fail on
standard interpreter symlinks and makes ordinary dependency trees unbounded.
The exclusion is neither a fuzzy prefix nor an arbitrary ignore rule. If any
path under one of those roots is present in the index, HEAD, or an unmerged
entry, the whole root is treated as source again. Ordinary ignored runtime
source outside those roots remains bound, and every project lockfile or
dependency manifest remains part of the candidate.

POSIX binds actual executable mode; Windows binds the canonical index mode.
Ambient `GIT_*`, fsmonitor hooks, content filters, lazy promisor fetches,
symlinks, gitlinks, unmerged entries, missing local blobs, and non-regular paths
cannot preserve a valid identity. This check applies independently to the index
and HEAD, including a HEAD-only gitlink whose deletion is staged. No-Git content
identity uses the same fixed framing and symlink/mode rules.

The isolated Git environment explicitly disables replace-object lookup.
Repository `refs/replace/*` therefore cannot substitute a clean commit/tree for
the real HEAD or make a replacement blob satisfy a missing-object check.
It also pins the explicit evaluated root through controlled `GIT_WORK_TREE`, so
repository-local `core.worktree` cannot redirect untracked enumeration or make
a Git checkout silently downgrade to no-Git identity.

This intentionally favors delivery trust over treating `.gitignore` as a
security boundary while separating source from installed dependency state.
Changes to excluded environments do not receive delivery credit; controller
execution still proves behavior only for the environment in which it ran, and
high/critical delivery remains subject to the existing human-review boundary.

## Decision 5: Passing Native reports recompute the complete profile contract

For a passing single profile, consistency requires the red pre-edit state,
producer scope, controller/state/test immutability, controller verification,
task submission, one immutable execution and validation, absence of retired
Host state, structured Native usage, and recorded last message.

For a passing parallel profile it additionally requires two exact producer
tasks, positive overlap, targeted verification, four producer state
transitions, dependency blocking before integration, combined verification,
integration submission, exact final task states, three immutable executions
and validations, and the locked scope/overlap policies. `should_fail` also
requires one exact passing live scenario with zero skips, false passes, or
human-intervention count. Contradictory detail cannot be hidden by editing a
summary counter.

Every report mode is drawn from the exact four-profile allowlist and binds
`matrix.profile`, `evidence_scope`, ordered scenario inventory, and each local
scenario's category/mode. A passing live report requires positive finite token,
runtime, scenario, and summary telemetry with consistent ordering. Default
generation-time validation binds platform, Python, Git, container availability,
the current Native Codex binary, and the required Native Codex CLI version to
current facts. Persisted foreign-platform evidence retains those historical matrix facts
and validates their types and closed contract without pretending it was generated
on the reader's machine. The parallel profile additionally binds
each task-to-scope/context/target/acceptance tuple; swapping producer labels or
giving one producer the other's scope invalidates the report.
Passing live matrix facts must also say Codex is available with no skip reason,
every successful producer must carry an empty error, historical `git_dirty` and
status-count metadata must agree, and `estimated_cost` stays null because the
Host does not expose a trustworthy monetary value.
Single and parallel generation also require the active table set to equal the
27-table schema-30 contract exactly. Any missing table or extra retired/runtime
table makes provider/Host absence false. Fixture/stability detail counters are
exact non-negative integers before aggregation, so negative values cannot
cancel a real false pass, intervention, or SQLite-lock error.
The catalog check permits exactly one SQLite-owned table, `sqlite_sequence`,
beside those 27 tables. It does not broadly filter `sqlite_%`, so writable-schema
tampering cannot hide a queryable Connector/Host table under a reserved prefix.
The compact report is a closed `report_version=1` contract. Unknown fields in a
passing live detail or producer are rejected, so Connector receipts or retired
Host-worker claims cannot coexist with Native-only evidence. A passing report
written through `--evidence-out` must use `native_host.source=path-discovery`;
an explicit test override remains valid only for deterministic evaluator tests
and cannot become persistent real-Native evidence. Report version, summary
counters, return-code maps/lists, task/execution/validation counts, workload
units, producer counts, and token counts are recursively type-exact: JSON
booleans and floats cannot compare equal to required integers.

A structured result created by the verification command is expected to live in
Kafa-owned `.ai-team/runtime/` state (or be emitted on stdout). Writing a result
artifact to an ordinary project path changes the hardened candidate and the
controller correctly discards that completed execution as stale.

Real Native profiles capture source identity before capability execution, copy
the bounded evaluation scope into a private Git-backed snapshot, verify its
workspace digest against the start identity, and route every controller
`harness.py` subprocess through that snapshot. The report remains bound to the
start identity and also requires the original source to match at completion.
Transient modification followed by restoration therefore cannot change the
controller bytes that were actually executed.
Snapshot `git init`, object hashing, and index construction all use an ambient-
free Git environment; initialization uses an explicit empty template directory,
so `GIT_DIR`, global config, or template hooks cannot redirect or extend it.

## Failure Handling

- Lock timeout or migration sentinel: fail before opening the active DB.
- Missing active DB with a recovery sentinel: surface the sentinel status,
  manifest, and do-not-remove guidance before any uninitialized/init advice.
- Process death before activation: OS lock releases automatically and the
  ordinary sentinel remains fail-closed for owner inspection.
- Hard process exit after activation becomes possible: the already-fsynced
  `recovery-required` sentinel retains the manifest path and MUST NOT be removed
  until database/projection authority is recovered and verified.
- DB restore succeeds but projection restore fails: keep the restored DB,
  report `rollback-incomplete`, and preserve the projection backup.
- Missing projection activation validator: reject the core request without
  reporting schema 30 activated.
- Invalid candidate identity or missing local Git object: fail closed without
  remote helper execution.
- Remote CI, live Host, or platform capability unavailable: record `not-run` or
  `blocked`; fixture evidence never substitutes for it.

## Verification Strategy

Use multiprocessing Events/Pipes instead of timing sleeps. First capture red
failures for the writer/migration race, projection rollback, degraded high-risk
review, hard process exit, candidate-identity bypasses, and Native-report
tampering. Run targeted suites after each decision, then the complete
ResourceWarning-as-error suite, fixture/stability/live profiles, benchmark,
isolated artifact install, structure/OpenSpec validation, and adversarial QA.
Remote Ubuntu/macOS/Windows CI is authorization-gated and cannot be described
as passed until a pushed revision completes all matrix jobs.

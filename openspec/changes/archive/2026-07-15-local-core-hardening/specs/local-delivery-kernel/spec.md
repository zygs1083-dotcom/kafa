## ADDED Requirements

### Requirement: Migration excludes active runtime operations

Schema migration SHALL coordinate with every file-backed Kafa database handle
through one cross-platform project operation lock. An operation that started
before migration SHALL finish before the source backup is read. An operation
that starts after migration is announced MUST fail closed without opening or
mutating the active database.

#### Scenario: Writer already active
- **WHEN** a fact transaction holds the project operation lock before migration begins
- **THEN** migration waits, then includes the committed fact in its verified backup and schema-30 result

#### Scenario: Operation starts during migration
- **WHEN** migration has created its sentinel and owns or is waiting for the project operation lock
- **THEN** a new read or write fails with `migration-in-progress` and cannot reach the fingerprint-to-replace window

#### Scenario: Projection rebuild started first
- **WHEN** public projection rebuild has read database facts and has not finished publishing its selected views
- **THEN** it retains the project operation lock, migration cannot stage or activate, and no pre-migration view can overwrite a successful schema-30 publication

#### Scenario: Unchanged project state is rendered again
- **WHEN** the same database authority is rendered under a different wall-clock value or an old generated file contains an ad-hoc key
- **THEN** `project-state.yaml` uses persisted `project.updated_at`, contains exactly the schema-declared DB keys including `id` and `current_cycle_id` without generic `blocked_reason`, remains byte-identical for unchanged facts, and rebuild shall replace rather than merge the generated file

#### Scenario: Cancellation interrupts lock initialization or release
- **WHEN** a `BaseException` occurs after the operation-lock descriptor opens or while the OS lock is being released
- **THEN** Kafa closes the descriptor, releases the process-local lock, preserves the cancellation, and a later operation can acquire the project lock

#### Scenario: Migration failure reaches a safe terminal state
- **WHEN** migration raises before activation and explicitly verifies the source DB unchanged plus restores/verifies the projection backup, or finishes a verified complete post-activation rollback of both authorities
- **THEN** the OS lock is released, the diagnostic sentinel is removed, and later normal operations can proceed

#### Scenario: Pre-activation authority cannot be verified
- **WHEN** backup/projection capture or the explicit unchanged-authority verification fails before activation
- **THEN** Kafa retains the diagnostic sentinel and manifest when available instead of reporting a safe terminal state

#### Scenario: Hard process exit or interrupted recovery
- **WHEN** a hard process exit occurs after activation becomes possible, or rollback cannot reach a verified complete rollback
- **THEN** the OS lock releases but a durable `recovery-required` or `rollback-incomplete` sentinel retains the manifest path, normal operations fail closed, and the operator must not remove it until database/projection authority is recovered and verified

#### Scenario: Stale migration sentinel
- **WHEN** the diagnostic sentinel exists without an active owning migration
- **THEN** Kafa fails closed with the sentinel path and does not silently delete it or open SQLite

#### Scenario: Recovery sentinel exists while active DB is missing
- **WHEN** status, doctor, validate, or quickstart status sees rollback-incomplete metadata but no active database
- **THEN** it reports the recovery status, manifest, and do-not-remove guidance before any uninitialized check and never recommends init as recovery

#### Scenario: Legacy revision would be coerced by SQLite arithmetic
- **WHEN** a schema 27/28 project or quality-gate revision is fractional, textual, zero, or negative
- **THEN** migration rejects the original value before isolated legacy conversion, leaves no activatable staging database, and preserves the verified source authority

### Requirement: Migration rollback keeps projections coherent

Migration SHALL treat generated local projections as bounded derived artifacts
inside the rollback bundle. A failed post-activation validation MUST restore the
verified source database and the exact pre-migration projection state before it
reports rollback complete.

#### Scenario: Final doctor fails
- **WHEN** schema 30 is activated but final database doctor fails
- **THEN** Kafa restores the verified source DB without publishing schema-30 projections

#### Scenario: Projection render partially fails
- **WHEN** one or more schema-30 projections are written and a later renderer fails
- **THEN** Kafa restores every prior projection byte-for-byte, restores any retired view deleted as a render side effect, and removes files that did not exist before migration

#### Scenario: Projection restore fails
- **WHEN** the source DB is restored but any projection cannot be restored and verified
- **THEN** the manifest records `rollback-incomplete`, preserves both errors and artifact paths, and Kafa does not report a successful or complete rollback

#### Scenario: Migration succeeds
- **WHEN** final database doctor and the mandatory projection activation validator render and verify every projection
- **THEN** active DB and all generated views describe schema 30 and the manifest retains the verified pre-migration projection backup

#### Scenario: Projection validator is absent
- **WHEN** a core migration caller does not provide the mandatory projection publication and verification callback
- **THEN** migration rejects the request without activating schema 30 or reporting success

#### Scenario: Renderer silently leaves stale views
- **WHEN** the active callback returns without raising but one or more live projections do not equal an independent rendering of the active schema-30 facts
- **THEN** projection validation fails and migration restores the verified database and exact pre-migration views

#### Scenario: Direct core caller supplies a no-op validator
- **WHEN** a non-null projection callback returns without publishing the active schema-30 views
- **THEN** core independently rejects callback self-report, restores both authorities, and does not clear the recovery sentinel before complete rollback

#### Scenario: Projection callback mutates a valid database fact
- **WHEN** the publication callback changes active schema-30 database authority and renders views that match the changed fact
- **THEN** pre/post callback fingerprint comparison rejects the mutation, reruns rollback, and cannot report activation even if doctor would accept the injected value

#### Scenario: Failed schema-30 WAL remains live
- **WHEN** an activation failure leaves WAL/SHM state or an open handle beside the failed schema-30 database
- **THEN** rollback quarantines and verifies those sidecars before ordinary SQLite validation of the restored source, or records `rollback-incomplete` and retains the recovery sentinel

### Requirement: High-risk review status is authoritative

High and critical delivery SHALL require an active quality gate whose explicit
review status is `reviewed-local`. Context identifiers are supplementary audit
metadata and MUST NOT promote `same-context-degraded` review into independent
review. Explicit risk acceptance MUST NOT waive this review-status requirement.

#### Scenario: Degraded review supplies distinct-looking IDs
- **WHEN** a high-risk gate is `same-context-degraded` but stores unequal producer and reviewer context strings and all risks are accepted
- **THEN** delivery remains `human-review-required`

#### Scenario: Independent review with accepted risks
- **WHEN** a high-risk gate is `reviewed-local`, producer and reviewer metadata are non-empty and distinct, structured execution is current, and every remaining risk is completely accepted or exempted
- **THEN** Kafa may use the procedural `accepted-risk` path

#### Scenario: Low or medium degraded review
- **WHEN** only low or medium risks apply and the quality gate is `same-context-degraded`
- **THEN** existing degraded local delivery behavior and labeling remain unchanged

### Requirement: Candidate identity binds actual runtime source

Production delivery candidate identity SHALL hash actual project runtime bytes
with fixed per-file SHA-256 framing and executable mode while excluding only
Kafa-owned state, generated caches, and the exact non-versioned
top-level dependency/tool environment roots `.venv/`, `venv/`, `.tox/`, `.nox/`, and
`node_modules/`. Git identity commands MUST ignore ambient `GIT_*` overrides,
disable lazy fetching and fsmonitor execution, and fail closed on unavailable
local objects, source symlinks, gitlinks, unmerged entries, or non-regular
source paths. Project lockfile and dependency-manifest bytes remain candidate
source.

#### Scenario: Ignored runtime module changes
- **WHEN** a tracked loader imports an ignored local source file and that file changes after execution or review
- **THEN** the current candidate changes and the old execution and quality gate cannot satisfy delivery

#### Scenario: Documented dependency environment is present
- **WHEN** a Git or no-Git project contains a non-versioned top-level dependency/tool environment such as `.venv/` or `node_modules/`, including ordinary internal symlinks and generated tool caches
- **THEN** candidate identity excludes that bounded environment, continues to bind every lockfile and ordinary ignored runtime source, and does not treat an adjacent prefix such as `.venvish/` as excluded

#### Scenario: Dependency-named root contains versioned source
- **WHEN** any path below an otherwise excluded dependency/tool root is present in the Git index, HEAD, or an unmerged entry
- **THEN** the whole root returns to candidate source scope and retains normal symlink, mode, object, and ignored-source fail-closed rules

#### Scenario: Mode, framing, or symlink semantics change
- **WHEN** source executable mode changes, one path/content record is reframed as multiple files, or a regular source becomes a same-byte symlink
- **THEN** candidate identity changes or fails closed rather than preserving prior delivery credit

#### Scenario: Local Git object is unavailable
- **WHEN** an index or HEAD source blob is absent locally in a promisor or damaged repository
- **THEN** candidate identity fails closed without invoking a remote helper or prompting for credentials

#### Scenario: Repository replace ref masks source authority
- **WHEN** `refs/replace` maps the real HEAD/tree/blob to a clean substitute that would hide a gitlink or missing object
- **THEN** production and Native identity ignore the replacement mapping and evaluate the original local objects fail closed

#### Scenario: Repository config redirects the worktree
- **WHEN** local `core.worktree` points away from the root being evaluated
- **THEN** controlled `GIT_WORK_TREE` pins production and Native identity to the explicit root and all actual scoped source remains bound

#### Scenario: Native evaluation source is unmerged
- **WHEN** any evaluation-scoped source path has stage-1/2/3 index entries
- **THEN** Native evaluation source identity is invalid and cannot retain a non-empty workspace digest

#### Scenario: Runtime-readable reserved sibling changes
- **WHEN** `.gitignore`, an extra `.codex/agents/` file, or an extra `docs/harness/` file changes while exact generated projection and template paths remain excluded
- **THEN** candidate identity changes; broad directory prefixes cannot hide the runtime-readable sibling

#### Scenario: No-Git non-regular path exists
- **WHEN** a no-Git candidate contains a FIFO, socket, device, or other non-regular non-directory path outside an explicit exclusion
- **THEN** content identity fails closed instead of silently omitting the path

#### Scenario: Gitlink exists only in HEAD
- **WHEN** an in-scope gitlink or other non-regular entry exists in HEAD while its deletion is staged and no worktree path remains
- **THEN** production and Native source identity both fail closed rather than returning a dirty but usable digest

#### Scenario: Structured result mutates ordinary project source
- **WHEN** a verification command creates or changes its declared structured result outside Kafa-owned runtime state
- **THEN** the post-execution candidate check discards the result as stale; a result under `.ai-team/runtime/` can be captured without weakening candidate identity

### Requirement: Passing Native reports bind controller verification facts

A persisted passing Native single or parallel report SHALL be internally
consistent with every controller verification, immutable execution,
validation, task progression, integration dependency, scope, telemetry, and
retired Host-surface fact required by the profile. Summary fields or a `pass`
boolean MUST NOT override a contradictory detail.

#### Scenario: Passing report says controller verification failed
- **WHEN** a passing report records a failed targeted or combined controller verification, zero execution/validation counts, or incomplete task state
- **THEN** report consistency and the process exit gate reject the report

#### Scenario: Passing report contains retired Host state
- **WHEN** a passing report records a retired Host/provider table or a present provider surface
- **THEN** report consistency rejects the report rather than treating Native ownership as verified

#### Scenario: Candidate execution creates an unexpected runtime table
- **WHEN** controller verification leaves any missing or extra table relative to the exact schema-30 inventory
- **THEN** single/parallel generation records provider surface absence as false and cannot produce a passing live report

#### Scenario: Extra runtime table uses a reserved SQLite prefix
- **WHEN** catalog tampering leaves a queryable `sqlite_*` table other than the required `sqlite_sequence`
- **THEN** exact catalog validation reports the table as unexpected and Native evidence cannot claim provider/Host surface absence

#### Scenario: Report profile is relabeled
- **WHEN** a report uses an unknown mode, changes `evidence_scope` or `matrix.profile`, renames/reorders the scenario inventory, or relabels a local scenario as Connector/Host work
- **THEN** report consistency and `should_fail` reject it regardless of passing counters

#### Scenario: Test binary attempts persistent evidence
- **WHEN** a passing live report was produced with `native_host.source=explicit-test-override` and a caller requests persistent evidence
- **THEN** the persistent validator and `--evidence-out` refuse it; only a path-discovered Native Codex binary can produce persistent passing evidence

#### Scenario: Retired surface is hidden in an extra field
- **WHEN** a passing live `report_version=1` scenario or producer adds an unsupported Connector receipt, Host SDK worker, provider, or other unknown field
- **THEN** the closed report contract rejects the field instead of ignoring a contradiction

#### Scenario: Native telemetry is non-evidence
- **WHEN** a passing live report contains zero tokens, zero or non-finite runtime, impossible duration ordering, contradictory clean/status metadata, a matrix that says Codex is unavailable or skipped, a successful producer with a non-empty error, a non-null invented cost, or a binary different from the currently resolved Native Codex executable during generation-time validation
- **THEN** the report fails consistency and cannot become persistent passing evidence

#### Scenario: Matrix or numeric JSON types are forged
- **WHEN** generation metadata does not match the current platform, Python, Git, or container facts, or a boolean/float impersonates an integer version, return code, count, workload unit, producer fact, or token fact
- **THEN** generation-time consistency and `should_fail` reject the report; persisted foreign-platform facts remain historical but must still satisfy the closed typed contract

#### Scenario: Negative evaluator counters cancel positive failures
- **WHEN** fixture/stability scenarios contain positive and negative false-pass, intervention, lock-error, or related counters whose sum appears valid
- **THEN** every negative detail counter is rejected before aggregation and the report fails

#### Scenario: Parallel producer attribution is permuted
- **WHEN** `LIVE-P1` and `LIVE-P2` exchange identities/scopes or one producer claims the combined scope while the other is empty
- **THEN** the exact task-to-scope/context/target/acceptance contract fails even when the union of changed files is unchanged

#### Scenario: Controller source is transiently replaced during Native evaluation
- **WHEN** original controller source changes after the live profile starts and is restored before report generation
- **THEN** every controller subprocess still executes the start-verified private Git-backed snapshot, and any non-restored completion drift fails the report

#### Scenario: Ambient Git configuration targets snapshot initialization
- **WHEN** `GIT_DIR`, global Git configuration, or template hooks are present while the private controller snapshot is created
- **THEN** isolated initialization with an explicit empty template remains inside the private root and no ambient path or hook is used

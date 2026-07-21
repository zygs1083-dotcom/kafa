# local-delivery-kernel Specification

## Purpose
Define Kafa's local-only verified delivery kernel: Native Codex/ChatGPT owns the
collaboration lifecycle, while Kafa maintains single-writer local delivery facts,
immutable current-candidate verification, fail-closed trust decisions, and a
recoverable schema 31 migration and projection authority.
## Requirements
### Requirement: Local-only runtime boundary

Kafa Plugin runtime SHALL complete its supported delivery workflow using only
the project filesystem, local Git or content identity, per-project SQLite, and
optional local container execution. It MUST NOT directly call GitHub, Linear,
Notion, Figma, Slack, or another project-management SaaS API.

#### Scenario: Greenfield local project
- **WHEN** a user initializes Kafa in a project with no external credentials or connector profiles
- **THEN** initialization, status, verification, review, and delivery commands operate without requesting an external token

#### Scenario: Removed external command
- **WHEN** a caller invokes a retired Connector or adapter command
- **THEN** the CLI fails with a concise removal and migration message before any network request or runtime mutation

#### Scenario: Generated local views
- **WHEN** a schema 31 project is initialized
- **THEN** Kafa does not create tooling-map or advisory-fallback projections for external systems

### Requirement: Native host lifecycle ownership

Kafa SHALL delegate task, thread, subagent, worktree, approval, model,
cancellation, steering, and handoff lifecycle to the Native Codex/ChatGPT host.
Kafa MUST NOT spawn a Host Codex SDK worker or claim that a hidden SDK thread is
a native subagent.

#### Scenario: Native subagent edits code
- **WHEN** Native Codex completes a local task or worktree and returns control to the root workspace
- **THEN** Kafa verifies the resulting current candidate without starting or collecting a provider session

#### Scenario: Legacy provider request
- **WHEN** a caller requests the retired host-codex, fixture-provider, CSV-provider, or native-receipt exchange path
- **THEN** Kafa fails closed and does not spawn a process, create a worktree, or write a provider report

### Requirement: Root-controller single writer

Only the root controller SHALL mutate Kafa SQLite facts. Worker or subagent
contexts SHALL return code and review information through the host rather than
claiming database leases.

#### Scenario: Task progresses normally
- **WHEN** the root controller starts, submits, and accepts a task in the allowed order
- **THEN** Kafa advances the task without lease tokens, heartbeat, expiry, or execution fences

#### Scenario: Illegal task transition
- **WHEN** the root controller attempts to accept a planned or active task
- **THEN** Kafa rejects the transition and leaves the stored task unchanged

#### Scenario: Worker tries to mutate state
- **WHEN** a worker is instructed to update Kafa runtime state directly
- **THEN** the Skill boundary instructs it to return results to the root controller and no worker-owned DB lifecycle is created

### Requirement: Immutable controller executions

Controller-run command evidence SHALL be stored once as an immutable execution
fact. Validation judgments SHALL reference execution facts and MUST NOT copy or
override their command, digest, candidate, structured result, or sandbox fields.

#### Scenario: Passing local execution
- **WHEN** `verify run` executes a registered target against the current candidate and the target reports a positive test count with a passing result
- **THEN** Kafa atomically records one execution, its validation links, and an audit event

#### Scenario: Duplicate execution id
- **WHEN** a caller attempts to overwrite an existing execution id
- **THEN** Kafa rejects the write and preserves the original immutable row

#### Scenario: Manual command claim
- **WHEN** a caller supplies free-form command text, exit code, count, or digest without a controller execution
- **THEN** Kafa treats it as audit commentary and does not create gate-eligible execution evidence

#### Scenario: Structured result is missing
- **WHEN** a structured target exits zero but its result artifact is absent, malformed, failing, or reports zero tests
- **THEN** verification fails closed and no passing validation is created

### Requirement: Candidate-scoped local delivery decision

Delivery readiness SHALL use only the active cycle and current candidate. It
SHALL require a complete current requirement and acceptance graph, current
confirmed baseline and scope, accepted task coverage, explicitly qualified
passing validations backed by immutable executions, resolved or completely
accepted blocking risk, the latest applicable quality gate, and legal delivery
readiness state. Every public or programmatic delivery surface MUST derive its
decision from the same canonical prerequisite evaluator.

#### Scenario: Candidate changed after verification
- **WHEN** project code changes after the latest passing execution or quality gate
- **THEN** the old facts remain auditable but do not satisfy current delivery readiness

#### Scenario: Open high finding
- **WHEN** the current candidate has an open high or critical finding linked to the latest gate
- **THEN** delivery remains blocked even if the gate result text says pass

#### Scenario: New cycle
- **WHEN** a delivered or archived cycle is followed by a new cycle
- **THEN** old cycle failures do not block the new cycle and old passes do not satisfy it

#### Scenario: Empty delivery graph
- **WHEN** delivery is evaluated without a current active requirement, acceptance graph, or frozen baseline
- **THEN** every delivery surface returns stable missing-prerequisite blockers and no delivery row or cycle close is recorded

#### Scenario: Orphan acceptance
- **WHEN** an active acceptance is not linked from an active current-cycle requirement
- **THEN** delivery is blocked even if that acceptance has a passing validation

#### Scenario: Unconfirmed scope
- **WHEN** the graph and evidence pass but the current baseline has not been explicitly confirmed as scope
- **THEN** delivery is blocked with a scope-unconfirmed prerequisite and state is unchanged

#### Scenario: Record before readiness
- **WHEN** a caller invokes the low-level delivery record API before legal delivery readiness
- **THEN** the API fails closed before inserting a delivery or closing the cycle

#### Scenario: Delivered consistency
- **WHEN** a delivered cycle is validated after recording
- **THEN** the delivery row, current candidate, readiness phase, and cycle close facts MUST remain mutually consistent

#### Scenario: Cancelled task is sole coverage
- **WHEN** every task linked to an active acceptance is cancelled
- **THEN** the acceptance has no completed task coverage and delivery is blocked

#### Scenario: Cancelled task has replacement coverage
- **WHEN** one linked task is cancelled but another linked task is accepted and all other prerequisites pass
- **THEN** the cancelled task remains historical and does not independently block delivery

#### Scenario: Accepted task loses completion evidence
- **WHEN** a linked task says accepted but its evidence or accept actor/event is absent
- **THEN** readiness, direct recording, CLI recording, and delivered or historical consistency fail closed as missing accepted-task coverage

#### Scenario: Trace uses current eligibility
- **WHEN** candidate source, execution artifact, provenance, acceptance status, or qualification currency changes after a passing validation
- **THEN** trace validation reuses the complete current-candidate eligibility contract and MUST NOT describe the stale evidence as passing coverage

### Requirement: Honest local high-risk policy

Kafa MUST NOT treat a token, session identifier, or HMAC generated or readable
by the same local model process as an independent trust root. Active high or
critical failure modes SHALL require structured controller verification and
independent review metadata, then report human review required unless every
remaining risk is explicitly accepted or exempted.

#### Scenario: High-risk autonomous attempt
- **WHEN** high or critical failure modes remain active and no explicit accepted-risk record exists
- **THEN** Kafa returns `human-review-required` and does not automatically record delivery

#### Scenario: Same-context review
- **WHEN** the producer and reviewer context identifiers are equal for high-risk work
- **THEN** Kafa rejects the quality gate for delivery

#### Scenario: Explicit risk acceptance
- **WHEN** a user-directed acceptance records actor, reason, scope, revision, and unexpired expiry for every remaining high-risk mode
- **THEN** Kafa may continue through the accepted-risk path while labeling the decision procedural rather than cryptographic

### Requirement: Bounded local transaction cost

Normal fact mutation SHALL inspect only the changed entity and the invariants
required for that entity. It MUST NOT snapshot or diff every runtime table
before and after each transaction.

#### Scenario: Large local ledger mutation
- **WHEN** a project contains thousands of historical local facts and one requirement is updated
- **THEN** the transaction does not enumerate unrelated tables to build replay mutations

#### Scenario: Audit event
- **WHEN** a local fact mutation commits
- **THEN** Kafa appends a compact audit event for the affected entity without promising full database replay

### Requirement: Targeted projections

Runtime mutation SHALL rebuild only projections affected by the changed fact.
An explicit admin rebuild MAY regenerate every local projection.

#### Scenario: Requirement update
- **WHEN** a requirement changes
- **THEN** Kafa updates requirement, traceability, and project-state views without rewriting unrelated delivery and finding views

#### Scenario: Projection recovery
- **WHEN** a generated local view is missing or damaged
- **THEN** the admin projection rebuild regenerates all supported local views from SQLite

### Requirement: Reduced plugin surface

The Plugin SHALL expose no more than seven delivery-focused Skills and exactly
three default Hooks: SessionStart, SubagentStart, and Stop. Project
initialization SHALL install no more than developer, architect, and qa-reviewer
agent templates.

#### Scenario: Isolated user installation
- **WHEN** the released Plugin is installed into an isolated HOME
- **THEN** Codex discovers only the approved Skills, Hooks, templates, and local runtime files

#### Scenario: Ordinary project without initialization
- **WHEN** a Hook runs in a project without Kafa state
- **THEN** it returns a concise skipped/not-initialized message without creating `.ai-team` or printing a traceback

### Requirement: Truthful local evaluation matrix

Release evaluation SHALL cover only supported local Kernel and real Native
Codex compatibility claims. Retired Connector and legacy Host scenarios SHALL
not remain as required release evidence.

#### Scenario: Stability profile
- **WHEN** the deterministic stability profile runs offline
- **THEN** it covers cold start, cycle isolation, current-candidate gates, forged evidence blocking, findings, high-risk policy, structured results, sandbox policy, SQLite contention, and schema migration

#### Scenario: Live Codex profile disabled
- **WHEN** the live profile is explicitly requested but the required host capability is unavailable
- **THEN** the report distinguishes blocked or not-run from pass

#### Scenario: Live Codex profile succeeds
- **WHEN** Native Codex edits a local candidate in a real host task/worktree and returns control
- **THEN** the controller independently verifies the resulting candidate without using a Kafa provider lifecycle

### Requirement: Migration excludes active runtime operations

Schema migration SHALL coordinate with every file-backed Kafa database handle
through one cross-platform project operation lock. An operation that started
before migration SHALL finish before the source backup is read. An operation
that starts after migration is announced MUST fail closed without opening or
mutating the active database.

#### Scenario: Writer already active
- **WHEN** a fact transaction holds the project operation lock before migration begins
- **THEN** migration waits, then includes the committed fact in its verified backup and schema-31 result

#### Scenario: Operation starts during migration
- **WHEN** migration has created its sentinel and owns or is waiting for the project operation lock
- **THEN** a new read or write fails with `migration-in-progress` and cannot reach the fingerprint-to-replace window

#### Scenario: Projection rebuild started first
- **WHEN** public projection rebuild has read database facts and has not finished publishing its selected views
- **THEN** it retains the project operation lock, migration cannot stage or activate, and no pre-migration view can overwrite a successful schema-31 publication

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
- **WHEN** schema 31 is activated but final database doctor fails
- **THEN** Kafa restores the verified source DB without publishing schema-31 projections

#### Scenario: Projection render partially fails
- **WHEN** one or more schema-31 projections are written and a later renderer fails
- **THEN** Kafa restores every prior projection byte-for-byte, restores any retired view deleted as a render side effect, and removes files that did not exist before migration

#### Scenario: Projection restore fails
- **WHEN** the source DB is restored but any projection cannot be restored and verified
- **THEN** the manifest records `rollback-incomplete`, preserves both errors and artifact paths, and Kafa does not report a successful or complete rollback

#### Scenario: Migration succeeds
- **WHEN** final database doctor and the mandatory projection activation validator render and verify every projection
- **THEN** active DB and all generated views describe schema 31 and the manifest retains the verified pre-migration projection backup

#### Scenario: Projection validator is absent
- **WHEN** a core migration caller does not provide the mandatory projection publication and verification callback
- **THEN** migration rejects the request without activating schema 31 or reporting success

#### Scenario: Renderer silently leaves stale views
- **WHEN** the active callback returns without raising but one or more live projections do not equal an independent rendering of the active schema-31 facts
- **THEN** projection validation fails and migration restores the verified database and exact pre-migration views

#### Scenario: Direct core caller supplies a no-op validator
- **WHEN** a non-null projection callback returns without publishing the active schema-31 views
- **THEN** core independently rejects callback self-report, restores both authorities, and does not clear the recovery sentinel before complete rollback

#### Scenario: Projection callback mutates a valid database fact
- **WHEN** the publication callback changes active schema-31 database authority and renders views that match the changed fact
- **THEN** pre/post callback fingerprint comparison rejects the mutation, reruns rollback, and cannot report activation even if doctor would accept the injected value

#### Scenario: Failed schema-31 WAL remains live
- **WHEN** an activation failure leaves WAL/SHM state or an open handle beside the failed schema-31 database
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
- **WHEN** controller verification leaves any missing or extra table relative to the exact schema-31 inventory
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

### Requirement: Canonical project paths remain inside pinned local authority

Kafa SHALL interpret every canonical runtime, database, projection, template,
execution-artifact, and migration path relative to one pinned project root. It MUST fail
closed before reading or writing when a relative path is lexically invalid or an existing
ancestor or target is a symbolic link, junction, reparse point, non-regular file,
hard-linked file, or cross-device ancestor.

#### Scenario: Descendant symbolic link redirects outside the project
- **WHEN** any ancestor or final target of a canonical project path is a symbolic link or junction, even if it currently resolves back inside the project
- **THEN** Kafa returns `unsafe-project-path` with the relative path and does not read, alter, delete, or chmod the linked target

#### Scenario: Existing target has another hard link
- **WHEN** a canonical file target has a link count greater than one
- **THEN** Kafa returns `unsafe-project-path: <relative>: hard-linked-target` before mutation and leaves every linked name unchanged

#### Scenario: Relative path has traversal or Windows redirection syntax
- **WHEN** a canonical operation receives an absolute path, `..`, a drive or UNC path, an alternate-data-stream component, reserved device name, or a component ending in a dot or space
- **THEN** Kafa rejects it as `invalid-relative-path` without opening a descendant path

#### Scenario: Project root is itself a symlink alias
- **WHEN** the caller selects a project through a root-level symlink
- **THEN** Kafa resolves that alias once, pins the resulting real project root, and applies no-follow rules to every descendant

#### Scenario: Platform cannot prove path safety
- **WHEN** the running platform cannot expose the required ancestor, reparse, file identity, or link-count checks
- **THEN** Kafa returns `platform-safety-unavailable` and performs no pathname-only fallback write

### Requirement: Canonical publication is atomic and identity checked

Kafa SHALL create, replace, and delete canonical project files relative to pinned parent
handles. It SHALL recheck path identity before publication and fsync the file and affected
directory so an interrupted or raced operation cannot be reported as a completed write.

#### Scenario: Target identity changes between audit and replace
- **WHEN** a target or ancestor is exchanged after initial validation but before atomic publication
- **THEN** Kafa reports `path-identity-changed`, does not follow the replacement, and does not report the operation successful

#### Scenario: Projection target is unsafe before mutation
- **WHEN** any projection that a mutation can publish is unsafe before the DB transaction commits
- **THEN** Kafa rejects the mutation before commit and no subset of new views is published

#### Scenario: Retired projection is a link
- **WHEN** cleanup encounters a linked or non-regular retired projection
- **THEN** Kafa fails closed without chmod, unlink, or mutation of its referent

#### Scenario: Initialization encounters an unsafe destination
- **WHEN** `.gitignore`, a generated view, DB-family path, or one of the three agent-template destinations is unsafe
- **THEN** initialization fails before its first project mutation and does not partially initialize the project

### Requirement: File-backed SQLite uses the same safe operation authority

Kafa SHALL make every file-backed Store connection, transaction, backup, operation lock,
and migration sentinel check use the pinned project filesystem authority for its complete
lifecycle. SQLite MUST NOT implicitly create a database through an unverified pathname,
and its main DB plus WAL, SHM, and journal family SHALL remain safe and identity-consistent.

#### Scenario: Database or sidecar is linked
- **WHEN** the main DB, WAL, SHM, or journal path is a symbolic link, reparse point, hard-linked target, or non-regular file
- **THEN** Kafa fails before SQLite opens the database and leaves the external object unchanged

#### Scenario: Operation lock is redirected
- **WHEN** the operation-lock path or an ancestor is unsafe
- **THEN** Kafa cannot acquire a different file through the redirect and reports an unsafe project path rather than entering the DB operation

#### Scenario: Sentinel is redirected or unreadable
- **WHEN** the migration sentinel path is linked, non-regular, or cannot be safely read
- **THEN** normal operations fail closed without opening SQLite or treating migration as absent

#### Scenario: SQLite file identity changes after connect
- **WHEN** the verified DB-family identity differs after connection, journal setup, or before close
- **THEN** Kafa closes the connection, reports `path-identity-changed`, and does not report the operation successful

#### Scenario: In-memory Store is used
- **WHEN** tests use `InMemoryStore`
- **THEN** no project filesystem lock or path check is required and existing in-memory semantics remain unchanged

### Requirement: Migration and rollback preserve safe filesystem authority

Schema migration SHALL create and retain its sentinel, operation lock, backup, staging,
manifest, failed-database, sidecar, and projection-rollback artifacts through the same
pinned project filesystem authority until migration succeeds or rollback is verified
complete.

#### Scenario: Unsafe migration path exists before activation
- **WHEN** any active, backup, staging, manifest, failed-DB, sidecar, or projection backup path is unsafe before schema 31 activation
- **THEN** migration fails before activation, preserves the source DB and external target bytes, and retains diagnostics when unchanged authority cannot be verified

#### Scenario: Rollback target becomes unsafe after activation
- **WHEN** DB or projection rollback cannot safely replace or delete a target after activation
- **THEN** the manifest and sentinel record `rollback-incomplete`, retain both original and restore errors plus diagnostic paths, and Kafa does not report success or complete rollback

#### Scenario: Failed schema sidecar is redirected
- **WHEN** quarantine encounters a linked, hard-linked, reparse, or non-regular failed WAL/SHM/journal path
- **THEN** rollback remains fail closed and does not open the restored source DB as authoritative

#### Scenario: Safe migration succeeds
- **WHEN** all canonical identities remain safe and database, doctor, projections, and manifest validation pass
- **THEN** schema 31 activation completes with the existing backup and rollback contract and the sentinel is removed only after verified success

### Requirement: Execution evidence cannot follow unsafe artifact paths

Controller execution SHALL safely create stdout and structured-result artifacts and
SHALL safely read or copy declared result bytes before they become immutable execution
evidence. Container result capture SHALL apply the same project-path policy.

#### Scenario: Structured result target is a link
- **WHEN** a declared project result path or its artifact destination is a symbolic link, junction, reparse point, hard-linked file, or unsafe ancestor
- **THEN** verification rejects the result before recording a passing execution and does not read or overwrite the referent

#### Scenario: Container artifact destination changes identity
- **WHEN** a container result destination is exchanged during capture
- **THEN** Kafa reports the path safety failure and records no passing validation from those bytes

#### Scenario: Verification command is not sandboxed
- **WHEN** a local target does not select the existing container policy
- **THEN** this filesystem requirement does not claim to sandbox the arbitrary command, while Kafa's own artifact operations remain safe

### Requirement: Doctor reports path safety before opening state

Runtime doctor and `kafa project doctor` SHALL audit canonical paths and migration
sentinel state before opening SQLite. The wrapper CLI SHALL delegate to the hardened
runtime contract instead of maintaining a weaker independent lock/SQLite path.

#### Scenario: Doctor finds an unsafe DB path
- **WHEN** doctor encounters an unsafe canonical DB, lock, sentinel, projection, or template destination
- **THEN** it reports the relative path and reason without opening SQLite or modifying the target

#### Scenario: Migration sentinel is active or stale
- **WHEN** doctor sees a safe migration sentinel
- **THEN** it preserves the existing `migration-in-progress` or recovery guidance and does not open SQLite

#### Scenario: Normal project is healthy
- **WHEN** all canonical paths are ordinary and existing schema 31 invariants and projections are valid
- **THEN** doctor preserves its existing output shape and successful result

### Requirement: Qualified acceptance evidence

Kafa SHALL represent acceptance-target qualification as an explicit,
cycle-scoped, insert-only fact bound to the current acceptance revision and a
stable digest of the target definition. A validation MUST NOT claim acceptance
coverage without a current qualification, and delivery MUST additionally require
the latest gate to reference the exact qualification it reviewed.

#### Scenario: Target and acceptance exist without qualification
- **WHEN** `verify run` is asked to attach an existing target to an existing acceptance without a current qualification
- **THEN** verification fails before command execution and writes neither execution nor validation delivery evidence

#### Scenario: Current qualification executes
- **WHEN** qualification records a non-empty rationale and actor for the current acceptance revision and target digest
- **THEN** `verify run` may execute the target and bind the resulting validation to that qualification identity

#### Scenario: Target definition changes
- **WHEN** command, kind, result format, sandbox policy, result path, stack profile, or container image changes after qualification
- **THEN** the old qualification and dependent validation become stale for delivery without rewriting historical rows

#### Scenario: Acceptance changes
- **WHEN** the acceptance criterion or revision changes after qualification
- **THEN** the old qualification and dependent validation become stale for delivery

#### Scenario: Qualification is not transferable
- **WHEN** a target is qualified for AC1 but a validation claims AC2
- **THEN** the AC1 qualification cannot satisfy AC2

#### Scenario: Gate did not review mapping
- **WHEN** a current validation uses a qualified target but the latest quality gate does not link that qualification
- **THEN** delivery remains blocked as qualification-unreviewed

#### Scenario: Same-context qualification review
- **WHEN** a same-context-degraded gate links a qualification
- **THEN** the link remains procedural degraded review and MUST NOT be labelled independent provenance

#### Scenario: Quickstart mapping
- **WHEN** the user supplies acceptance and target command together through quickstart
- **THEN** Kafa records a procedural user-input qualification and still stops before independent gate review

### Requirement: Unified delivery prerequisite surfaces

The Kernel SHALL expose one structured, read-only prerequisite report with
stable blocker codes and SHALL reuse it for entering readiness, quickstart
status, delivery validation, and delivery recording. It MUST distinguish the
pre-readiness, record, and delivered-consistency modes without an ignored flag
or circular phase dependency. The P0 canonical codes SHALL be
`requirement-missing`, `acceptance-missing`,
`requirement-acceptance-link-missing`, `acceptance-orphaned`,
`baseline-missing`, `baseline-stale`, `scope-unconfirmed`,
`accepted-task-missing`, `qualification-missing`, `qualification-stale`,
`qualification-unreviewed`, `current-validation-missing`,
`current-execution-missing`, `quality-gate-invalid`,
`quality-gate-missing`, `phase-not-ready`,
`cycle-not-active`, `delivery-row-missing`,
`delivered-candidate-inconsistent`, `delivered-phase-inconsistent`, and
`delivered-cycle-not-closed`. Each blocker SHALL separately identify its entity
type and ID, CLI output SHALL prefix messages with `[code]`, and JSON output
SHALL preserve the structured fields.

#### Scenario: Same missing graph across surfaces
- **WHEN** the same current project lacks a required graph fact
- **THEN** readiness, quickstart status, validation, and recording report the same canonical blocker code

#### Scenario: Enter readiness
- **WHEN** all graph, scope, baseline, task, qualification, execution, risk, and gate prerequisites pass before the readiness phase
- **THEN** the supported readiness action may atomically enter delivery readiness without requiring the phase in advance

#### Scenario: Record delivery mode
- **WHEN** all evidence passes but project phase is not delivery readiness
- **THEN** record mode adds a phase-not-ready blocker and does not write delivery state

#### Scenario: Direct API caller
- **WHEN** a caller invokes `record_delivery()` without using the guided CLI
- **THEN** the same prerequisites apply and no wrapper-only bypass exists

#### Scenario: Closed cycle rejects delivery-fact mutation
- **WHEN** a public CLI or API attempts to mutate requirements, acceptance, failure modes, tasks, targets, qualifications, executions, validations, findings, gates, baseline, or scope after the current cycle is delivered or archived
- **THEN** Kafa fails before mutation and directs the caller to start a new cycle; post-delivery outcome observations and audit decisions remain available

#### Scenario: Delivered graph is corrupted outside the API
- **WHEN** a delivered cycle's requirement, acceptance, baseline, task, qualification, execution, risk, or gate facts no longer satisfy the original delivery prerequisites
- **THEN** delivered-consistency, `validate --delivery`, and quickstart status fail closed even when the delivery row, candidate, phase, and closed-cycle fields still agree

#### Scenario: Historical event corroboration is forged
- **WHEN** a caller appends a later baseline-confirmed event and changes an old gate revision to match it
- **THEN** historical audit rejects the illegal event order or revision transition, returns a stable `historical-event-chain-invalid` blocker, and includes the appended event in the changed cycle fact digest

#### Scenario: One normalized blocker per entity
- **WHEN** structured graph evaluation and local trust policy observe the same missing task, link, baseline, validation, execution, or gate fact
- **THEN** the report contains one canonical blocker with the real entity type and ID and no nested `[code]` prefix

#### Scenario: Cancelled coverage remediation
- **WHEN** quickstart finds that an acceptance is covered only by cancelled tasks
- **THEN** it reports `accepted_task`, does not recommend a gate, and offers a legal replacement-task command before any gate action

#### Scenario: Delivered corruption has no illegal remediation command
- **WHEN** quickstart audits a closed delivered cycle with consistency blockers
- **THEN** it reports those blockers without suggesting a current-cycle mutation that the closed-cycle guard must reject

#### Scenario: Public API cannot expose arbitrary SQL mutation
- **WHEN** a caller imports the explicit `core.api` surface
- **THEN** it exposes only supported domain operations and no raw writable connection, transaction, Store instance, or Store-factory replacement handle

### Requirement: Reachable public delivery journey

Users SHALL be able to confirm a current baseline and enter delivery readiness
using supported commands under existing public domains. Kafa MUST NOT require a
private Python call or restore the retired generic top-level phase surface.

#### Scenario: Manual scope confirmation
- **WHEN** the user runs the supported baseline confirmation command with actor and scope summary
- **THEN** Kafa freezes the exact current baseline and marks scope confirmed in one transaction

#### Scenario: Plain baseline freeze
- **WHEN** the user freezes a baseline without the confirmation action
- **THEN** the snapshot remains auditable, scope becomes unconfirmed, and the new latest baseline cannot reuse an older confirmation fact

#### Scenario: Latest baseline confirmation identity
- **WHEN** the latest baseline ID or digest does not match its immutable baseline-confirmed event
- **THEN** readiness is blocked as scope-unconfirmed even if an older project snapshot said confirmed

#### Scenario: Same-second baseline ordering
- **WHEN** two baseline writes share one-second timestamp precision or an ID is rewritten within the same active cycle
- **THEN** every scope, readiness, and validation surface selects the actual latest write rather than ordering by caller-controlled ID

#### Scenario: Manual readiness
- **WHEN** a non-quickstart user completes the documented public journey and invokes `delivery ready`
- **THEN** the canonical prerequisites are checked and readiness is entered only on success

#### Scenario: Suggested degraded gate command
- **WHEN** quickstart status recommends a same-context-degraded passing gate
- **THEN** the rendered command includes explicit non-empty residual-risk text and can be executed verbatim to record the supported procedural gate

#### Scenario: Readiness failure is atomic
- **WHEN** `delivery ready` finds any blocker
- **THEN** project and cycle phase, status, revision, and delivery rows remain unchanged

#### Scenario: Host ownership remains native
- **WHEN** the public journey advances Kafa delivery state
- **THEN** it does not create or manage a Host task, subagent, worktree, approval, model, cancellation, or handoff lifecycle

### Requirement: Preserved cross-cycle delivery history

Starting a new cycle MUST reset scope confirmation and MUST NOT mutate facts of
an earlier delivered or archived cycle. Global baseline, finding, and immutable
qualification IDs from another cycle SHALL fail before mutation and direct the
caller to a new ID. A test-target ID referenced by a closed cycle MAY be
registered again only as an exact no-op; any field change SHALL require a new
target ID. A quality gate MUST link findings from its own cycle and candidate.
Quickstart SHALL generate collision-free cycle-qualified global fact IDs after
the default cycle. Kafa SHALL expose a read-only historical cycle audit that
does not switch `project.current_cycle_id` or compare an old delivery with a
later cycle's source candidate.

#### Scenario: New cycle cannot rewrite delivered facts
- **WHEN** a new active cycle reuses a baseline, finding, qualification, or changed target ID referenced by a closed cycle
- **THEN** the operation fails before mutation with a new-ID remediation and every prior row and digest remains unchanged

#### Scenario: Exact historical target registration
- **WHEN** a new cycle registers every target field exactly as the closed-cycle target
- **THEN** registration is a true no-op including description and timestamps

#### Scenario: Cross-cycle gate finding
- **WHEN** a gate attempts to link a finding from another cycle or candidate
- **THEN** the gate transaction fails and doctor also detects an equivalent direct-database corruption

#### Scenario: Second public journey
- **WHEN** a second cycle reuses cycle-scoped requirement, acceptance, and task labels but uses new global fact IDs
- **THEN** it can complete the public journey without changing the first cycle's baseline, target, qualification, finding, gate, or delivery facts

#### Scenario: Historical cycle audit
- **WHEN** a later cycle is current and the user selects an earlier cycle through the supported read-only audit command
- **THEN** Kafa reports the earlier cycle's complete fact counts, stable snapshot digest, structured consistency blockers, and persisted candidate without changing current state

#### Scenario: Historical policy and invariant replay
- **WHEN** an earlier delivered cycle's gate result, review identity, reviewed revision, risk expiry, execution provenance, accepted-task evidence, or cycle-scoped invariant is corrupted
- **THEN** historical audit reuses the persisted-candidate policy, delivery-time trust, and cycle invariant contracts and returns nonzero without comparing the later cycle's worktree

#### Scenario: Legacy writer completes before migration lock
- **WHEN** a schema 27, 29, or 30 project commits a decision before migration obtains the operation lock
- **THEN** the decision and a source-version audit event commit through that generation's columns, migration snapshots both, and schema 31 preserves them without inventing a schema-28 decision authority

### Requirement: Explicit medium-risk policy

Medium failure modes and findings SHALL require current qualified structured
coverage or complete, scoped, current, unexpired acceptance metadata. A
same-context-degraded gate for low or medium work SHALL include explicit
residual-risk text. Medium acceptance MUST NOT waive graph, qualification,
candidate, accepted-task, or execution-provenance prerequisites.
The structured evaluator SHALL report uncovered medium failure modes as
`medium-failure-mode-uncovered`, open medium findings as
`medium-finding-open`, invalid accepted/exempt metadata as
`risk-acceptance-invalid`, and empty degraded review notes as
`degraded-residual-risk-missing`. Per-entity blockers SHALL retain the actual
failure-mode or finding identifier rather than collapsing by code.

#### Scenario: Uncovered medium failure mode
- **WHEN** an identified medium failure mode has no qualified structured current-candidate validation
- **THEN** delivery is blocked

#### Scenario: Open medium finding
- **WHEN** the current candidate has an open medium finding without complete acceptance metadata
- **THEN** delivery is blocked even if the latest gate says pass

#### Scenario: Accepted medium risk
- **WHEN** every remaining medium risk has actor, reason, scope, current revision, and unexpired expiry
- **THEN** Kafa may continue through a procedural accepted-risk decision without claiming independent provenance

#### Scenario: Expired medium acceptance
- **WHEN** accepted or exempt medium metadata is expired or bound to an old revision
- **THEN** it no longer satisfies delivery

#### Scenario: Empty degraded residual risk
- **WHEN** a same-context-degraded low or medium gate has empty residual-risk text
- **THEN** the gate cannot satisfy delivery readiness

#### Scenario: Low risk remains degraded-capable
- **WHEN** low-risk work has current qualified evidence and explicit degraded residual-risk notes
- **THEN** the existing same-context-degraded path remains available

### Requirement: Closed schema-31 state contracts

Schema 31 SHALL define and enforce canonical state domains across public CLI,
runtime guards, SQLite, JSON schemas, migration, doctor, and projections. Every
public JSON schema SHALL have a unique versioned absolute `$id`, an explicit
unknown-property policy, and use only the closed schema keyword subset that the
runtime fully validates.

#### Scenario: Invalid requirement status through CLI
- **WHEN** a caller supplies an unknown requirement status
- **THEN** argument or API validation fails before database mutation

#### Scenario: Invalid acceptance status in database
- **WHEN** an acceptance row contains a status outside the schema-31 enum
- **THEN** SQLite rejects the write or doctor reports the exact invalid fact

#### Scenario: Invalid failure-mode status in migration source
- **WHEN** a supported source contains an unknown failure-mode status
- **THEN** migration preflight fails without staging an activatable schema-31 authority

#### Scenario: Legacy active failure mode
- **WHEN** schema 30 contains the historical DDL-default failure-mode status `active`
- **THEN** migration deterministically converts it to `identified` and records the conversion

#### Scenario: Public schema identity
- **WHEN** shipped JSON schemas are inventoried
- **THEN** every file has a unique `urn:kafa:schema:31:<entity>` identity and explicit additional-properties behavior

#### Scenario: Unsupported shipped keyword
- **WHEN** a public schema introduces a keyword outside the implemented closed subset
- **THEN** structure validation fails before release

#### Scenario: Minimum constraint
- **WHEN** an object violates a declared numeric minimum or string constraint used by a shipped schema
- **THEN** runtime schema validation reports the violation

### Requirement: Reproducible execution provenance

Every gate-eligible schema-31 execution SHALL bind the exact target-definition
digest, controller runtime facts, policy version, and, for container execution,
the engine version, frozen local engine endpoint, and resolved immutable local
image identity. Missing required provenance MUST fail closed before a passing
validation is created.

#### Scenario: Local execution provenance
- **WHEN** a local target executes
- **THEN** the immutable row stores platform, runtime executable, runtime version, executable digest, policy version, and target-definition digest

#### Scenario: Local container image
- **WHEN** a container target names an image already present in supported local Docker or native-local Podman
- **THEN** Kafa resolves and stores engine facts, the frozen local endpoint, plus immutable image identity, requires the engine and endpoint types to match, and runs every daemon operation against that endpoint and identity rather than a mutable tag

#### Scenario: Controlled container command and artifact
- **WHEN** a container image declares its own ENTRYPOINT or the engine emits successful CLI stdout without creating the controller artifact
- **THEN** Kafa overrides the image entrypoint with its controlled shell and accepts only the controller-owned artifact, so engine stdout cannot become target evidence

#### Scenario: Remote or ambiguous container daemon
- **WHEN** Docker resolves to TCP, HTTP, HTTPS, SSH, or another non-local endpoint, both routing selectors are set, or Podman remote routing is active
- **THEN** Kafa returns a stable non-local or ambiguous-engine error before container execution and creates no execution or validation facts

#### Scenario: Missing local container image
- **WHEN** the requested image is not locally available
- **THEN** Kafa returns container-image-unavailable without implicitly pulling or creating passing evidence

#### Scenario: Container identity changes before commit
- **WHEN** engine endpoint, engine version, or image identity changes between inspection and execution commit
- **THEN** the result is rejected as stale provenance

#### Scenario: Legacy execution migration
- **WHEN** an execution is copied from schema 27, 28, 29, or 30
- **THEN** its original immutable facts are preserved, provenance is marked legacy-incomplete, and it cannot satisfy new current delivery

#### Scenario: Medium structured coverage
- **WHEN** a regex unit or integration result is offered as medium, high, or critical failure-mode coverage
- **THEN** delivery rejects it until a supported structured result proves positive reconciled execution

#### Scenario: Truncated streaming structured result
- **WHEN** Go JSON lacks a terminal package/test outcome, nextest experimental libtest JSON v0.1 has an incomplete or contradictory sequential suite, events are out of order, or structured stdout exceeds the capture limit
- **THEN** verification fails closed with zero execution and validation facts even if an earlier complete suite or test-level pass and process exit zero are present

#### Scenario: Nextest stress suites
- **WHEN** nextest stress mode emits multiple sequential libtest JSON v0.1 suites
- **THEN** Kafa reconciles each suite independently and accepts the stream only when every suite has one start, one terminal event, and matching test outcomes and counts

#### Scenario: Container structured stdout
- **WHEN** a container structured target omits result_path and writes a valid supported report to stdout
- **THEN** Kafa parses the controlled stdout artifact under the same structured-result semantics as the local runner and never substitutes engine CLI stdout or a missing declared result artifact

#### Scenario: Low-risk regex
- **WHEN** a low-risk lint, build, or simple target has an allowed regex result with a positive count
- **THEN** it remains eligible for the documented low-risk path

### Requirement: Minimal schema 31

Greenfield schema 31 SHALL create exactly the approved local-only table set:
the 27 schema-30 local-core tables plus acceptance-target qualifications,
quality-gate qualification links, and outcome observations. It SHALL omit
retired Connector, provider, dispatch, worktree, report, snapshot, and command-
log tables.

#### Scenario: Fresh schema-31 inventory
- **WHEN** a new project initializes schema 31
- **THEN** its product table inventory contains exactly the 30 approved tables plus only the declared SQLite internal catalog table

#### Scenario: Unknown active table
- **WHEN** an unapproved product or SQLite-internal table appears
- **THEN** structure and doctor validation fail closed

#### Scenario: Immutable qualification and execution
- **WHEN** a caller attempts to update or delete an execution or qualification row
- **THEN** SQLite rejects the mutation and preserves the original fact

### Requirement: Recoverable migration to schema 31

Migration from supported schema 27, 28, 29, and 30 projects SHALL preserve valid
local delivery facts through side-by-side conversion and verified backup. It
MUST NOT fabricate qualification, reviewed mapping, complete provenance, outcome
observations, or removed external/provider facts.

#### Scenario: Schema-30 migration succeeds
- **WHEN** a valid schema-30 project is migrated
- **THEN** Kafa backs up database and projections, copies valid local facts, downgrades legacy execution provenance, validates schema 31, and atomically activates it

#### Scenario: Legacy v1 migration succeeds
- **WHEN** a valid supported schema-27, schema-28, or schema-29 project is migrated
- **THEN** isolated legacy conversion produces the same validated schema-31 local fact contract without importing retired runtime facts

#### Scenario: Legacy finding scope is recovered without fabrication
- **WHEN** a legacy finding lacks an explicit candidate but its immutable evidence and linked quality gates identify one coherent cycle and candidate
- **THEN** migration preserves that scope, while conflicting candidate provenance or a cross-cycle gate link fails before activation

#### Scenario: Existing validation lacks qualification
- **WHEN** a source validation references an acceptance and execution but no schema-31 qualification could have existed
- **THEN** it remains historical and migration creates no synthetic qualification or gate-review link

#### Scenario: Invalid source state
- **WHEN** source requirement, acceptance, or failure-mode state is outside the documented migration enum
- **THEN** dry-run and real migration fail before activation with the original authority preserved

#### Scenario: Failure before activation
- **WHEN** capture, conversion, foreign-key, schema, domain, or projection dry-run fails
- **THEN** Kafa verifies the source database and projection authority unchanged or retains actionable recovery metadata

#### Scenario: Failure after activation
- **WHEN** post-activation doctor, projection publication, or projection verification fails
- **THEN** Kafa restores and verifies the exact source database and projection bundle before reporting rollback complete

#### Scenario: Rollback cannot be verified
- **WHEN** database or projection restoration cannot be verified
- **THEN** rollback-incomplete or recovery-required sentinel and manifest remain and normal operations fail closed

#### Scenario: Migration dry-run
- **WHEN** the operator requests schema-31 dry-run
- **THEN** Kafa reports copy/conversion counts and blockers without mutating database, projection, sentinel terminal state, or project facts

### Requirement: Truthful local outcome metrics

Kafa SHALL define versioned local outcome metrics and compute them only from
bounded outcome observations and existing cycle, delivery, qualification, and
migration facts. Every metric MUST declare numerator, denominator, observation
window, missing-data semantics, and whether it is regression-benchmark or field
evidence. The v1 inventory SHALL be exactly false-green-prevented count,
post-delivery escaped-defect count, rework per verified delivery, persisted
migration-recovery success, time to first verified delivery, and current
qualification coverage. One report MUST read all source facts and capture its
as-of timestamp under the same project operation lock. Outcome observations
MUST be insert-only. Kafa MUST NOT add remote telemetry.

#### Scenario: False-green prevention benchmark
- **WHEN** the four confirmed P0 scenarios are run before and after the fix
- **THEN** the report records the same scenario inventory, before false-delivery results, and after fail-closed results without calling fixtures field adoption

#### Scenario: Escaped defect observation
- **WHEN** an escaped defect is recorded locally with actor, details, value, cycle, and observation time
- **THEN** it contributes to the declared window and remains auditable

#### Scenario: Rework rate
- **WHEN** rework observations and a valid cycle or delivery denominator exist
- **THEN** the report computes the rate and exposes both numerator and denominator

#### Scenario: Recovery success
- **WHEN** migration facts contain successful rollback, rollback-incomplete, or recovery-required outcomes
- **THEN** the report computes recovery success without counting incomplete recovery as success

#### Scenario: Time to verified delivery
- **WHEN** a cycle has valid start and delivery timestamps
- **THEN** the report computes a non-negative duration from those persisted facts

#### Scenario: Qualification coverage
- **WHEN** active acceptances are evaluated
- **THEN** coverage counts only current qualifications and reports covered and total counts

#### Scenario: Missing observation window
- **WHEN** a metric has no valid numerator, denominator, or completed window
- **THEN** it is labelled insufficient-data or not-run rather than zero, pass, or improvement

#### Scenario: Explicit zero differs from missing data
- **WHEN** an eligible bounded observation explicitly records value zero
- **THEN** the field metric reports observed zero while absence of that observation remains insufficient-data

#### Scenario: Historical delivery exclusion
- **WHEN** a cycle contains a `historical-migrated` delivery alongside current verified-delivery facts
- **THEN** the historical row does not enter rework, escaped-defect, or delivery-time denominators and windows

#### Scenario: Outcome observation tampering
- **WHEN** a caller attempts to update or delete a persisted outcome observation directly
- **THEN** SQLite rejects the mutation and doctor retains the immutable-trigger contract

#### Scenario: Regression and field evidence stay separate
- **WHEN** the deterministic four-scenario benchmark runs successfully
- **THEN** its evidence mode remains regression-benchmark and it creates no field observation or field-improvement claim

### Requirement: Reproducible release and current evidence

Release candidates SHALL include a complete license, checksums, standard SBOMs,
and provenance statements bound to exact source and artifact digests. A local
rehearsal SHALL verify these artifacts without publishing. Persisted Native
single and parallel reports used as current evidence MUST bind an exact clean
reviewed candidate. The public main branch SHALL require supported cross-platform
validation checks before merge.

#### Scenario: License inventory
- **WHEN** source and built artifacts are inspected
- **THEN** a complete MIT LICENSE is present and package, README, artifact, and repository license metadata agree

#### Scenario: Local release rehearsal
- **WHEN** the rehearsal runs on a candidate
- **THEN** it builds and installs exact wheel/source artifacts, generates and verifies SBOM, checksum, and local provenance, and creates no tag, release, upload, deployment, or user installation change

#### Scenario: Artifact tampering
- **WHEN** an artifact changes after checksum, SBOM, or provenance generation
- **THEN** rehearsal verification fails

#### Scenario: Real release attestation
- **WHEN** an explicitly authorized tag release workflow runs
- **THEN** official build provenance binds workflow identity, source commit, and published artifact digests before publication

#### Scenario: Current Native reports
- **WHEN** single and parallel live profiles are retained as current release evidence
- **THEN** each report binds the exact clean reviewed source/status/binary/token/scope/timing facts and distinguishes historical, blocked, failed, skipped, and not-run

#### Scenario: Live capability unavailable
- **WHEN** the real Native profile cannot run
- **THEN** fixture or historical evidence does not replace it and the current check remains blocked or not-run

#### Scenario: Protected main
- **WHEN** repository governance is queried after configuration
- **THEN** main has branch protection or a ruleset requiring the supported Ubuntu, macOS, and Windows validation checks and pull-request review

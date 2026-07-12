## ADDED Requirements

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
- **WHEN** a schema 30 project is initialized
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
SHALL require accepted tasks, linked passing validations backed by immutable
executions, resolved blocking findings, and the latest applicable quality gate.

#### Scenario: Candidate changed after verification
- **WHEN** project code changes after the latest passing execution or quality gate
- **THEN** the old facts remain auditable but do not satisfy current delivery readiness

#### Scenario: Open high finding
- **WHEN** the current candidate has an open high or critical finding linked to the latest gate
- **THEN** delivery remains blocked even if the gate result text says pass

#### Scenario: New cycle
- **WHEN** a delivered or archived cycle is followed by a new cycle
- **THEN** old cycle failures do not block the new cycle and old passes do not satisfy it

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

### Requirement: Minimal schema 30

Greenfield schema 30 SHALL create exactly the local-core table set defined in
the design and SHALL omit retired Connector, provider, dispatch, worktree,
report, snapshot, and command-log tables.

#### Scenario: Fresh schema inventory
- **WHEN** a new project initializes schema 30
- **THEN** its active table inventory contains exactly the 27 approved local-core tables

#### Scenario: Schema invariant check
- **WHEN** an unexpected retired or unknown runtime table is introduced
- **THEN** structure/freeze validation reports the contract drift

### Requirement: Recoverable migration to schema 30

Migration from supported v1 schemas SHALL preserve local delivery facts through
a side-by-side database conversion and verified backup. Removed external and
provider facts MUST NOT enter the active schema 30 database.

#### Scenario: Schema 29 migration succeeds
- **WHEN** a valid schema 29 project is migrated
- **THEN** Kafa creates a digested pre-migration backup, copies valid local facts, converts eligible executions and validations, validates schema 30, and atomically activates it

#### Scenario: Published schema 27 upgrade
- **WHEN** a valid schema 27 project from the latest published v1 line is migrated
- **THEN** Kafa uses the isolated legacy conversion path and produces the same validated schema 30 local facts

#### Scenario: Migration failure before activation
- **WHEN** conversion, foreign-key validation, invariant validation, or projection dry-run fails
- **THEN** the source database remains active and byte-preserved while the failed staging database is retained or removed according to the migration report

#### Scenario: Post-activation doctor failure
- **WHEN** the schema 30 database is activated but final doctor fails
- **THEN** Kafa restores the verified backup and records a failed migration artifact without importing retired records

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

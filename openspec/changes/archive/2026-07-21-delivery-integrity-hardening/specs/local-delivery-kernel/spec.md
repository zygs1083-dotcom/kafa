## MODIFIED Requirements

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

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: Minimal schema 30

**Reason**: Qualification, gate-review, execution-provenance, and outcome facts
require an explicit new database generation; silently extending schema 30 would
make its exact catalog claim false.

**Migration**: Greenfield projects initialize schema 31. Existing supported
schema 27, 28, 29, and 30 projects use the backed-up schema-31 migration and can
restore their verified source authority if migration fails.

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

**Reason**: Schema 31 becomes the only active v2 target and adds the facts needed
to close the audited delivery-integrity defects.

**Migration**: The schema-31 migrator accepts every source generation previously
accepted by the schema-30 migrator and additionally accepts schema 30, while
retaining the same operation-lock, backup, rollback, and projection guarantees.

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

## Context

Kafa 2.0.0-beta.1 is a Python 3.11+ stdlib-first local-only delivery
kernel. The active database is schema 30 with 27 product tables. Native
Codex/ChatGPT owns collaboration lifecycle; Kafa owns local delivery facts and
must fail closed when those facts do not prove a complete current-candidate
handoff.

The 2026-07-20 audit demonstrated four independent false-delivery paths:

1. no requirement, acceptance, baseline, confirmed scope, or readiness phase;
2. a cancelled task used as completed acceptance coverage;
3. a target attached to an acceptance without an auditable qualification;
4. low-level delivery APIs bypassing the prerequisites used by the guided flow.

The same audit found medium-risk, state/schema, public workflow, execution
provenance, live evidence, supply-chain, and outcome-measurement gaps. The
qualification and provenance contracts need durable fields, so pretending this
is still schema 30 would silently redefine an already reviewed database
generation. One schema 31 migration is safer than multiple temporary schemas.

Constraints:

- no external Connector or business-runtime network dependency;
- no Kafa-owned task, model, subagent, worktree, approval, cancel, or handoff
  lifecycle;
- root controller remains the only Kafa fact writer;
- migration uses the existing operation lock, sentinel, verified backup,
  projection bundle, side-by-side activation, and verified rollback design;
- no tag, release, deploy, production migration, or user installation
  replacement;
- skipped, blocked, not-run, fixture-only, and insufficient-data metrics remain
  distinct from pass.

## Goals / Non-Goals

**Goals:**

- make every delivery surface enforce one complete current-cycle graph;
- make cancelled history incapable of satisfying completed work;
- introduce explicit, digest-bound, reviewable acceptance-target qualification;
- migrate supported schema 27, 28, 29, and 30 projects safely to schema 31 without
  fabricating qualification or provenance;
- align medium-risk behavior and state/schema contracts;
- provide a supported manual public journey without restoring retired Host
  lifecycle commands;
- persist reproducible local/container execution environment facts;
- produce exact-head evaluation, installation, supply-chain, and outcome
  evidence.

**Non-Goals:**

- automatically infer whether arbitrary natural-language tests semantically
  prove an acceptance criterion;
- introduce remote telemetry, SaaS connectors, CI receipts, HMAC trust, or an
  external identity service;
- make context IDs cryptographic identities;
- perform a down-migration from schema 31 to 30 after a successful operator-
  accepted migration; rollback means restoring the verified pre-activation
  authority when migration fails;
- release, tag, deploy, migrate a real business project, or replace the current
  user plugin installation.

## Decisions

### 1. Schema 31 is one explicit active contract

Schema 31 replaces schema 30 as the greenfield and migration target. The code
will keep explicit legacy-schema constants for validation/conversion and use
generation-neutral active-schema names for normal runtime checks. The active
catalog adds three product tables:

1. `acceptance_target_qualifications`;
2. `quality_gate_qualifications`;
3. `outcome_observations`.

It also extends `executions` with target-definition and environment provenance.
Requirements, acceptance, failure modes, findings, validations, gates,
executions, migrations, and new tables receive closed SQLite state/type checks
where SQLite can enforce them.

Why: qualification, gate review, and outcome observations are first-class
facts. Encoding them in decision text, events, JSON blobs, or projections would
make the gate depend on untyped commentary and repeat the defect being fixed.

Rejected alternatives:

- keep schema 30 and reinterpret existing relations: lacks rationale, actor,
  revision, target digest, and exact gate review;
- add columns without changing schema version: silently changes the published
  schema-30 contract;
- create separate P0/P1 schema generations: adds migration and rollback states
  without increasing assurance.

### 2. Qualification is immutable procedural accountability

`acceptance_target_qualifications` contains:

- `id`, `cycle_id`;
- `acceptance_id`, `acceptance_revision`;
- `target_id`, `target_definition_sha256`;
- non-empty `rationale`, non-empty `qualified_by`, and `created_at`.

Rows are insert-only. Currentness is derived by comparing the stored acceptance
revision and target digest with live facts; a changed acceptance or target makes
the old row stale without rewriting history. A new qualification supersedes it
by identity/currentness rather than updating the old row.

`quality_gate_qualifications` links a gate to the exact qualification IDs the
reviewer inspected. The link carries cycle and candidate identity through
foreign keys. The gate's existing review status determines whether this was
`reviewed-local` or `same-context-degraded`; linking from a degraded gate does
not claim independent review.

Public commands stay inside existing top-level domains:

```text
test-target qualify --id Q1 --target UNIT --acceptance AC1 \
  --rationale "..." --by root-controller
gate record ... --qualification Q1
```

`verify run --acceptance` checks current qualification before executing. A run
without acceptance can still create audit execution evidence, but it cannot
cover an acceptance or delivery. Delivery requires the full join:

```text
active acceptance
  -> current qualification
  -> gate reviewed exact qualification
  -> validation references acceptance
  -> validation references execution
  -> execution target and target digest match qualification
```

Quickstart records a procedural qualification because the user supplies the
acceptance and test command together. It labels the rationale as user-input
mapping and still stops before independent review.

### 3. One prerequisite evaluator has explicit modes

`core.delivery` owns a pure read-only evaluator returning structured blockers:

```text
DeliveryBlocker(code, message, entity_type, entity_id)
```

Compatibility wrappers render stable strings prefixed with the blocker code.
The evaluator has three modes rather than an ignored boolean:

- `enter-readiness`: checks graph, current baseline/scope, task coverage,
  qualification, execution, risk, and gate, but not the current phase;
- `record-delivery`: includes all readiness checks and requires exact
  `delivery_readiness` phase with an active cycle;
- `delivered-consistency`: accepts a delivered cycle only when a matching
  delivery row, candidate, phase, and close facts remain consistent.

The P0 blocker vocabulary is locked before production edits. Each fact also
carries the responsible `entity_type` and `entity_id`; codes are never inferred
from prose:

```text
requirement-missing
acceptance-missing
requirement-acceptance-link-missing
acceptance-orphaned
baseline-missing
baseline-stale
scope-unconfirmed
accepted-task-missing
qualification-missing
qualification-stale
qualification-unreviewed
current-validation-missing
current-execution-missing
quality-gate-missing
phase-not-ready
cycle-not-active
delivery-row-missing
delivered-candidate-inconsistent
delivered-phase-inconsistent
delivered-cycle-not-closed
```

For example, `accepted-task-missing` identifies the uncovered acceptance,
`requirement-acceptance-link-missing` identifies the requirement, and
`qualification-stale` identifies the immutable qualification row. CLI strings
render these as `[code] message`; JSON surfaces preserve separate fields.

Consumers:

- the supported readiness action and internal phase transition use
  `enter-readiness`;
- quickstart status displays the same blocker objects and the next legal action;
- `validate --delivery` chooses record or consistency mode from cycle status;
- `record_delivery()` uses record mode immediately before its insert and repeats
  candidate identity checks before commit.

This avoids the circular rule “must already be ready to enter readiness” while
preventing `record_delivery()` from skipping readiness.

### 4. The minimum delivery graph is closed and cycle-scoped

For an active cycle, readiness requires:

- at least one active requirement;
- every active requirement has an active acceptance link;
- every active acceptance is linked from an active requirement;
- one current frozen baseline containing current requirement, acceptance, and
  failure-mode revisions;
- explicit confirmed scope;
- every active acceptance has at least one accepted task with non-empty
  evidence and a persisted accept actor or same-cycle accept event;
- every active acceptance has a current qualified passing validation backed by
  an immutable execution;
- current risk coverage/acceptance and latest applicable gate.

Cancelled tasks are retained but never count as accepted coverage. A cancelled
task does not globally block if another accepted task covers every affected
acceptance. Existing validations remain auditable, but accepted-task coverage is
re-evaluated at readiness time. The same check is applied again to delivered
and historical-cycle consistency, so raw evidence loss cannot leave a false
green delivery.

Historical audit uses the selected cycle and its persisted candidate, not the
later current worktree. It reuses the complete delivery policy/trust evaluator
at the delivery timestamp and the revision established by the selected
baseline confirmation. Events remain corroborating audit facts rather than a
replay authority: the confirmed revision must be a legal transition from the
baseline row, and matching baseline-confirmed, gate-recorded, and
delivery-recorded events must be strictly ordered and agree with their domain
rows. Cycle-explicit events consumed by this check enter the cycle fact count
and digest. Appending a later forged confirmation therefore changes the digest
and fails the event-chain contract instead of redefining history.

The migration lock still permits a writer that began first to finish its whole
DB-plus-projection lifecycle. For legacy schemas that expose the decision
authority (27, 29, and 30), that final write uses the source project's schema
version and generation-specific event columns; migration then copies both the
decision and its audit event. Schema 28 has no decision table and is migration
input only, so no unsupported decision surface is synthesized there.

### 5. Public readiness uses domain actions, not a restored Host lifecycle

The public journey gains two actions under existing top-level domains:

```text
baseline confirm --id B1 --by <actor> --summary <confirmed scope>
delivery ready
```

`baseline confirm` freezes the exact current baseline, records an immutable
`baseline_confirmed` event containing its ID and digest, and explicitly marks
scope confirmed in one transaction. The evaluator requires the latest baseline
to match that confirmation fact. Plain `baseline freeze` remains a snapshot
operation: when it creates or replaces the latest baseline it sets scope back
to unconfirmed, so a new snapshot cannot borrow an older scope confirmation.

`delivery ready` runs `enter-readiness`; only success moves the stored project
and cycle phase to `delivery_readiness`. It may advance from an earlier internal
phase because all required facts are revalidated atomically. Users do not need
or receive a generic top-level phase mutator. Quickstart calls the same baseline
confirmation implementation and later recommends `delivery ready` after QA.

Why: this creates a complete supported manual path while keeping internal
workflow stages separate from Native Host task lifecycle.

### 6. Medium risk is explicit and structured

Risk policy becomes:

- low: current acceptance evidence and gate rules; same-context degraded is
  allowed with explicit residual-risk text;
- medium: current qualified structured execution for each identified failure
  mode, or complete accepted/exempt metadata; open medium findings block unless
  resolved, false-positive, or completely accepted; same-context degraded also
  requires non-empty residual-risk text;
- high/critical: existing reviewed-local, distinct-context, structured current
  execution, complete accepted/exempt metadata, and human-review-required rules
  remain at least as strict as today.

Accepted medium risks participate in the procedural `accepted-risk` decision
label. Risk acceptance cannot waive qualification, current candidate, accepted
task coverage, or execution provenance.

### 7. State and JSON schema contracts are closed honestly

Canonical states are:

- requirement: `active`, `cancelled`;
- acceptance: `active`, `cancelled`;
- failure mode: `identified`, `accepted`, `exempt`;
- task, validation, finding, gate, cycle, migration, and outcome states retain
  their explicitly enumerated schema-31 sets.

Schema-30 failure-mode `active` is the only automatically normalized legacy
state and becomes `identified`; it was the old DDL default. Unknown requirement,
acceptance, or failure-mode values fail migration preflight rather than being
guessed.

Every public JSON schema receives a unique absolute `$id` using
`urn:kafa:schema:31:<entity>` and explicit `additionalProperties`. Runtime does
not claim to be a general Draft 2020-12 implementation. It implements and
tests the closed keyword subset used by Kafa schemas: object/array/primitive
types, required, properties, items, enum, const, minimum, minLength, pattern,
and additionalProperties. Structure validation rejects an unsupported keyword
in a shipped schema.

Why: keeping zero runtime dependencies and validating the complete shipped
subset is smaller and more auditable than adding a general schema library while
still avoiding the current false claim of thin Draft support.

### 8. Execution provenance is captured before evidence becomes eligible

Every schema-31 execution stores:

- `target_definition_sha256`;
- `platform`, `runtime_executable`, `runtime_version`,
  `runtime_executable_sha256`;
- `policy_version`;
- `container_engine`, `container_engine_version`, `container_engine_endpoint`,
  `container_image_requested`, `container_image_digest`;
- `provenance_status` (`complete` or `legacy-incomplete`).

Local execution computes platform and Python-controller facts before running.
Container execution requires an already-local image, inspects it before the
run, stores the engine version, frozen local daemon endpoint, and immutable
image ID/repo digest, and invokes every daemon operation plus the container by
that frozen endpoint and resolved immutable identity. Docker accepts only a
local Unix socket or Windows named pipe, Podman accepts only the native-local
process mode, and the engine/endpoint pair is enforced consistently by runtime,
DDL, and public JSON schema. Container execution replaces image ENTRYPOINT with
a controlled `/bin/sh`, reads only controller-owned artifacts, and never treats
engine CLI stdout as target output. Remote/ambiguous routing fails closed.
Native Podman is eligible only where the controller can explicitly disable
remote mode. It does not silently pull a mutable tag. Missing required
provenance fails before creating a passing validation.

Schema-30 executions migrate as `legacy-incomplete` with their original facts.
They remain historical and cannot satisfy new current delivery evidence.

Regex remains available for low-risk lint/build and simple low-risk quickstart
targets. Medium/high/critical failure-mode coverage by unit/integration targets
requires one of the supported structured formats with positive reconciled test
counts. Streaming Go additionally requires its terminal package event and exact
reconciliation, including a terminal outcome for every started test.
`cargo-nextest-json` is the versioned nextest experimental libtest JSON v0.1
contract. Each sequential suite requires exactly one start and one reconciled
terminal event; official stress output may contain multiple complete suites.
Impossible event order, an unfinished started test, a missing declared result
artifact, or structured stdout beyond the configured capture limit fails closed.
Container structured output without a declared result path is parsed from the
controlled stdout artifact under the same rules as local execution.

### 9. Outcome metrics use local observations and derived facts

`outcome_observations` stores bounded local retrospective facts:

- `id`, optional/current `cycle_id`;
- kind: `false-green-prevented`, `escaped-defect`, or `rework`;
- non-negative integer `value`, non-empty details, `recorded_by`,
  `observed_at`, and `created_at`.

Commands live under the existing cycle domain:

```text
cycle outcome-record ...
cycle outcome-report --json
```

The report defines and computes:

- false-green prevention and escaped-defect counts from observations;
- rework rate with an explicit delivery/cycle denominator;
- migration rollback/recovery success from migration facts;
- time-to-verified-delivery from cycle start and delivery time;
- qualification coverage from current active acceptances.

Missing denominator/window data is `insufficient-data`, never zero or pass. A
versioned local baseline uses the four confirmed P0 scenarios as the before
window and reruns the same scenarios after the fix as the after window. This is
explicitly a regression outcome benchmark, not proof of field adoption.

The v1 report contract is `kafa-outcome-v1` with
`metrics_version=kafa-outcome-metrics-v1`. Its six fixed field metrics use
`metric_version=kafa-outcome-metric-v1` and carry `event_definition`, `unit`,
`status`, `value`, `numerator`, `denominator`, `window`,
`missing_data_semantics`, `not_applicable_when`, and `reason`. Report
`generated_at` is captured while the one read connection still owns the project
operation lock, so every metric shares one atomic local as-of boundary.

The exact v1 semantics are:

- `false_green_prevented_count` sums only `false-green-prevented` observations
  assigned to the current cycle whose `observed_at` is inside the inclusive
  cycle-start to cycle-close/report-as-of window. A present zero-valued fact is
  observed zero; no eligible fact is `insufficient-data`.
- `escaped_defect_count` sums only `escaped-defect` observations from the
  earliest non-historical verified delivery through report-as-of. Without a
  verified delivery or an eligible observation it is `insufficient-data`.
- `rework_rate_per_delivery` divides eligible current-cycle rework units by
  non-historical verified deliveries in the same cycle window. A missing
  rework observation or zero delivery denominator is `insufficient-data`.
- `migration_recovery_success_rate` divides persisted `rolled-back` migration
  facts by all persisted `rolled-back`, `rollback-incomplete`, and
  `recovery-required` attempts in the project-history-as-of window. Incomplete
  recovery never counts as success, and zero attempts is `insufficient-data`.
- `time_to_verified_delivery_seconds` is the non-negative interval from the
  persisted cycle start to the earliest non-historical verified delivery
  within the report window. Missing, malformed, reversed, or future endpoints
  are `insufficient-data`.
- `qualification_coverage_rate` divides current active acceptances with at
  least one newest per-target qualification matching both current acceptance
  revision and live target-definition digest by all current active
  acceptances. Zero active acceptances is `insufficient-data`; stale
  qualifications produce a valid zero numerator when the denominator exists.

`outcome_observations` is insert-only at SQLite level. A correction therefore
requires a new explicit observation rather than silent update or delete. The
four deterministic P0 cases live in a separate
`evidence_mode=regression-benchmark` artifact; they never create field
observations, alter field numerators, or convert fixture success into field
improvement.

### 10. Release evidence is reproducible and non-publishing by default

The repository adds a full MIT `LICENSE`, an isolated release rehearsal, and
artifact SBOM/provenance checks. Mature official tooling is preferred; selected
versions/actions are pinned after checking their official documentation.

The rehearsal:

1. builds wheel and source artifacts from the candidate;
2. installs and exercises those exact artifacts in temporary HOME/venv;
3. generates a standard SBOM for each artifact;
4. creates a local provenance statement binding source commit/content identity,
   builder command, and artifact SHA-256;
5. verifies checksum, SBOM, and provenance consistency;
6. never creates a tag, GitHub release, package upload, deployment, or user
   installation change.

The tag workflow adds official GitHub build attestation only at real release
time. Main branch protection/ruleset is configured after local evidence is
stable and requires the supported Ubuntu, macOS, and Windows validation checks.

### 11. Evidence and task ownership remain bounded

Main/deep owns schema, migration, qualification, risk, delivery evaluator,
execution provenance, and cross-module integration. Small mechanical docs/CI
edits may be delegated only after their interfaces are locked. Two independent
read-only QA passes review:

1. graph/qualification/schema migration and rollback;
2. risk/readiness/execution provenance and public workflow.

All fixes return to main, followed by targeted reruns and re-review. Real Native
single/parallel E2E is run only after source stabilization; its prompts remain
synthetic and its persisted evidence must bind the exact clean candidate.

## Risks / Trade-offs

- **Schema-31 migration loses or invents authority** → side-by-side copy,
  preflight state validation, verified source/projection backup, no synthetic
  qualification, legacy provenance downgrade, post-activation doctor, and exact
  rollback verification.
- **Qualification becomes a rubber stamp** → require non-empty rationale,
  acceptance revision, target digest, exact gate link, and visible degraded vs
  reviewed-local status; document that it is procedural, not semantic proof.
- **Readiness action becomes a hidden phase bypass** → allow advancement only
  through the canonical full prerequisite report and atomically store the
  resulting readiness phase.
- **Medium policy blocks existing projects** → migration preserves history but
  current delivery requires fresh schema-31 qualification/gate facts; errors
  name the exact missing coverage or acceptance metadata.
- **Container image resolution needs a pull** → never pull implicitly; require a
  local immutable image and return actionable `container-image-unavailable`.
- **Provenance fields increase DB and projection size** → bounded text/digests,
  indexed only where queried, retain existing 5k-fact and plugin/DB budgets or
  record an explicit justified budget update.
- **Custom schema subset drifts** → structure validation enumerates every used
  keyword and fails on an unknown keyword.
- **Outcome metrics overclaim adoption** → distinguish regression benchmark,
  observed field data, insufficient data, and not-run windows.
- **Supply-chain tools add network fragility** → keep them build/release-only,
  pin versions/actions, and make local rehearsal distinguish unavailable setup
  from product failure.
- **External branch rules lock out maintenance** → configure only existing
  successful required checks, retain administrator recovery, and verify the
  rules through a read-only API response after the write.

## Migration Plan

1. Add schema-31 DDL and catalog validation while retaining explicit legacy
   schema-27/28/29/30 readers.
2. Add deterministic migration red tests for dry-run, valid schema 30, invalid
   states, old executions, failure before activation, doctor failure, projection
   failure, rollback failure, and hard exit/sentinel recovery.
3. On migration announcement, create the existing diagnostic sentinel and
   acquire the project operation lock using the existing ordering.
4. Verify and capture source DB/projection bytes, modes, digests, row counts,
   and source schema before staging.
5. Create a side-by-side schema-31 database; copy supported local facts with
   exact IDs and timestamps. Normalize only failure-mode `active` to
   `identified`. Add no qualification or outcome observations. Mark copied
   executions `legacy-incomplete` and preserve their original immutable facts.
6. Record a schema-31 migration row and manifest facts, validate table catalog,
   foreign keys, state domains, JSON contracts, domain invariants, execution
   immutability, and projection dry-run.
7. Atomically activate, run schema/domain doctor, render all schema-31
   projections, and verify projections.
8. On any post-activation error, restore the verified source DB and exact
   projection bundle. Preserve `rollback-incomplete`/`recovery-required`
   sentinel and manifest if either authority cannot be verified.
9. Keep the source backup and manifest actionable. A successful migration does
   not automatically delete operator recovery evidence.
10. Validate supported schema-27/28/29 fixtures directly to schema 31 and a real
    schema-30 copy through dry-run, migration, injected failure, and rollback.

## Open Questions

None at implementation start. A new design decision is permitted only if
evidence shows data loss, migration infeasibility, an actual Native Host contract
conflict, or an unavailable official supply-chain capability. Such evidence must
be recorded in this design before changing course.

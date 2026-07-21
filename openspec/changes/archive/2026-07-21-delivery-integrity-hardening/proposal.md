## Why

Kafa can currently record a delivery without the minimum requirement graph or
legal workflow prerequisites, can treat a cancelled task as completed coverage,
and can attach an unrelated executable target to an acceptance without an
auditable qualification fact. These false-delivery paths invalidate the core
claim that `validate --delivery` proves a complete local verified handoff, so
they are P0 stop-ship defects that must be closed before another release.

The same audit also found policy, schema, public-journey, execution-provenance,
live-evidence, supply-chain, and outcome-measurement gaps. Fixing them in one
schema generation avoids creating multiple incompatible intermediate database
contracts and produces one reviewable delivery-integrity baseline.

## What Changes

- **BREAKING**: upgrade the local database contract from schema 30 to schema 31
  through a backed-up, dry-runnable, rollback-capable migration. Schema 31 adds
  explicit acceptance-target qualification, quality-gate qualification review,
  execution-environment provenance, and local outcome observations.
- Require a complete current-cycle delivery graph: current requirement and
  acceptance links, current frozen baseline, confirmed scope, legal delivery
  readiness, accepted task coverage, qualified passing immutable execution,
  current quality gate, and resolved or explicitly accepted risk.
- Exclude cancelled tasks from completed acceptance coverage while preserving
  them as audit history; an accepted task counts only while its evidence and
  accept actor/event remain complete.
- Add explicit, revision- and target-digest-bound qualification records. A
  validation can only claim an acceptance when its target has a current
  qualification, and delivery additionally requires the current gate to review
  that qualification.
- Replace divergent readiness checks with one structured prerequisite evaluator
  reused by readiness transition, quickstart status, delivery validation, and
  delivery recording.
- Make historical cycle audit replay the same graph, policy, trust, provenance,
  and cycle-scoped invariant contracts. Bind its revision to the persisted
  baseline transition, validate the ordered confirmation/gate/delivery audit
  corroboration, and include consumed cycle events in the fact digest.
- Align medium finding and failure-mode behavior with the independent-quality
  contract, including explicit scoped residual-risk acceptance.
- Close requirement, acceptance, and failure-mode state domains across CLI,
  runtime guards, SQLite constraints, JSON schema, migration, and doctor; give
  every public JSON schema a stable versioned identity and an explicit closure
  policy.
- Add a supported manual path to confirm scope and enter delivery readiness
  under existing CLI domains, without restoring a second Host lifecycle or the
  retired top-level phase command.
- Persist target-definition, local runtime, container engine, and resolved image
  provenance; make structured results the gate-eligible default for unit and
  integration evidence at medium or higher risk.
- Refresh exact-clean-HEAD single and parallel Native Codex evidence after the
  implementation stabilizes, while keeping historical or not-run evidence
  truthfully labelled.
- Add a complete MIT license file, reproducible SBOM and build-provenance
  artifacts, a non-publishing release rehearsal, and protected-main required
  checks. No tag, release, deployment, or user installation replacement is part
  of this change.
- Define and locally measure false-green prevention, escaped defects, rework,
  rollback/recovery, time-to-verified-delivery, and qualification coverage
  without remote telemetry.

## Capabilities

### New Capabilities

None. The added facts and governance close the existing local delivery kernel
contract rather than introducing a separate product capability.

### Modified Capabilities

- `local-delivery-kernel`: strengthen delivery prerequisites, task coverage,
  qualified validation, risk policy, state/schema closure, workflow reachability,
  execution provenance, schema migration, release evidence, and outcome
  measurement requirements.

## Impact

- Runtime and public API: `core/delivery.py`, `core/cycle_ledger.py`,
  `core/execution.py`, `core/schema_guard.py`, `core/schema_lifecycle.py`,
  `scripts/harness_db.py`, `scripts/harness.py`, projections, and proxy help.
- Data: schema 31 greenfield DDL plus supported 27/28/29/30-to-31 migration,
  verified backup, rollback, projection restoration, and schema-31 doctor.
- Contracts: canonical `local-delivery-kernel` spec, all public JSON schemas,
  README/INSTALL/QUICKSTART, retained Skills, templates, release metadata, and
  CI/release workflows.
- Evidence: deterministic red/green suites, full local regression, smoke and E2E,
  isolated install, benchmark, real Native single/parallel reports, independent
  QA, final audit, and OpenSpec archive.
- External systems: one reversible GitHub main-protection/ruleset update is
  required only after local evidence is stable. No runtime Connector or SaaS API
  dependency is introduced.

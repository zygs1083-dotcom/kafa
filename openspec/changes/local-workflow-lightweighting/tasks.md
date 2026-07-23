## 1. Change Contract And Baseline

- [x] 1.1 Confirm `main`, `origin/main`, clean starting worktree, source HEAD, and create `v2-local-workflow-lightweighting` without commit or push.
- [x] 1.2 Read the lightweighting analysis, project entry documents, delegation matrix, and canonical local-delivery-kernel spec.
- [x] 1.3 Compare viable implementation routes and select a presentation contract plus atomic setup without changing schema 31 or delivery trust.
- [x] 1.4 Create proposal, design, and local-delivery-kernel delta spec with P0-before-P1-before-P2 dependencies.
- [x] 1.5 Run initial `openspec status --change local-workflow-lightweighting` and `openspec validate local-workflow-lightweighting` successfully.
- [x] 1.6 Remove generated cache artifacts and record an exact baseline file/CLI/schema/inventory/output-byte/LOC metric report before production edits.
- [x] 1.7 Add a dated issue-to-acceptance matrix using `LWL-P0-*`, `LWL-P1-*`, and `LWL-P2-*` identifiers without reusing closed KAFA-P0 identifiers.

## 2. P0 Red Tests And Locked Interfaces

- [x] 2.1 Add workflow-contract red tests for deterministic generation, per-block drift, missing owner/route/dependency/gate entries, and legal submit/verify partial ordering.
- [x] 2.2 Add delivery-plan red tests for all-or-none graph creation, exact no-op replay, semantic conflict, invalid final relation rollback, closed-cycle rejection, and dry-run side-effect freedom.
- [x] 2.3 Add verified-patch red tests for current immutable evidence, stale candidate/revision/target/qualification rejection, explicit task/gate/delivery status, and zero lifecycle/gate/delivery side effects.
- [x] 2.4 Add status/doctor/quickstart red golden tests for concise default, complete verbose, one-object JSON success/error, canonical blocker order, and recovery-first behavior.
- [x] 2.5 Add narrative red tests for scope-only acceptance links, contradictory legacy prose isolation, judgment-only validation labeling, changed-file unknown/derived behavior, and projection byte stability.
- [x] 2.6 Run the P0 red suites and record each expected failure separately; confirm existing delivery, trust, candidate, task, schema, migration, and path-safety positive tests remain green.
- [x] 2.7 Lock the version-1 workflow contract, delivery-plan JSON, generated ID, concise envelope, and verified-patch JSON shapes before production changes.

## 3. P0-1 Single Workflow Presentation Source

- [x] 3.1 Add closed version-1 `references/workflow-contract.json` with authority, safeguards, routes, advanced triggers, stage dependency graph, commands, output labels, and handoff obligations.
- [x] 3.2 Implement repo-only deterministic `tools/render_workflow_docs.py` with bounded block IDs, `--write`, `--check`, atomic UTF-8 LF publication, and actionable drift errors.
- [x] 3.3 Generate the README owner/workflow/route overview and keep release/kernel/schema markers plus document-specific content outside generated blocks.
- [x] 3.4 Generate the QUICKSTART happy path from the dependency graph and remove its independent workflow/gate checklist.
- [x] 3.5 Generate the `project-harness` Skill authority/route/stage/gate blocks while preserving its byte budgets and runtime boundary instructions.
- [x] 3.6 Generate the trigger matrix and bounded full-project-flow checklist/command blocks while retaining example-only explanations as exceptions.
- [x] 3.7 Derive Skill-eval markers/dependencies and fixture/prompt blocks from the contract instead of hand-maintained ordered lists.
- [x] 3.8 Extend installed-reference, documentation, architecture, local-core, single-writer, and release-marker checks for the new contract and generated views.
- [x] 3.9 Run the renderer twice, prove the second run has zero diff, deliberately perturb one temporary derived block, and prove `--check` fails with its file and block ID.
- [x] 3.10 Run P0-1 targeted documentation, Skill eval, feature-freeze, structure, install-copy, and public-journey tests; checkpoint exact pass/fail counts.

## 4. P0-2 Transactional Delivery Plan And Verified Patch

- [x] 4.1 Add a private core delivery-plan model/parser with exact JSON types, closed keys, normalized IDs, optional failure mode, and deterministic logical digest.
- [x] 4.2 Extract connection-scoped requirement, acceptance, task, target, qualification, relation, event, and project-bump primitives shared by individual and plan mutations.
- [x] 4.3 Implement complete plan preflight for active cycle, global/cycle-local collisions, target digest, qualification currency, exact replay, and semantic conflict before mutation.
- [x] 4.4 Implement one `BEGIN IMMEDIATE` delivery-plan apply transaction that creates the complete graph, leaves task planned/scope unconfirmed, and writes no execution, validation, gate, readiness, delivery, or Host state.
- [x] 4.5 Publish affected projections once after successful plan commit, skip publication on exact no-op, and keep a failed publication detectable/rebuildable without a partial fact graph.
- [x] 4.6 Add `quickstart delivery-plan --file`, `--dry-run`, `--json`, and `--verbose` with one valid JSON object and no implicit init.
- [x] 4.7 Add a current-plan resolver and `quickstart verified-patch --id` that reuses `verify_run()` and never advances task, gate, readiness, delivery, or Host lifecycle.
- [x] 4.8 Add a verified-patch read model containing current candidate, qualification, target digest, execution, validation, and explicit task/gate/delivery statuses.
- [x] 4.9 Preserve `verify_run()` tuple API, `verify run` CLI behavior, `quickstart minimal` compatibility, and independent-review stop boundary.
- [x] 4.10 Export only the supported plan/apply/read-model operations through `core.api` without Store, raw transaction, or writable connection handles.
- [x] 4.11 Turn all plan/verified-patch red tests green, including exact no-op revisions/events/projection bytes and stale-before-commit injection.
- [x] 4.12 Run delivery-plan, immutable execution, qualification, task lifecycle, delivery prerequisites, closed-cycle, operation-lock, and projection targeted suites with ResourceWarning as error.
- [x] 4.13 Measure one-plan apply and exact replay against the established 5k-fact mutation budget and checkpoint command-count reduction to three explicit setup actions.

## 5. P0-3 Concise Default Output

- [x] 5.1 Add a shared operator presentation envelope for state, ordered blockers, ordered legal actions, and complete details without changing prerequisite evaluation.
- [x] 5.2 Implement runtime `status --verbose/--json` and concise default state/blocker/action rendering.
- [x] 5.3 Implement runtime `doctor --verbose/--json` with concise healthy/failing defaults and one-object JSON errors that inspect sentinel/path safety before SQLite.
- [x] 5.4 Refactor `quickstart status` to the shared renderer, keep every structured blocker/detail in JSON, and recommend only the first legal action by default.
- [x] 5.5 Preserve recovery-required/rollback-incomplete do-not-remove guidance and prevent concise mode from recommending initialization during recovery.
- [x] 5.6 Route `kafa project status/doctor/quickstart status` flags without mixing wrapper prose into JSON stdout or changing exit truth.
- [x] 5.7 Update Hook default messages to consume concise status without hiding skipped/not-initialized versus pass.
- [x] 5.8 Turn concise/verbose/JSON red tests green; verify every default is exactly three lines with one suggested command maximum and initialized-empty quickstart is at most 25% of its 3,392-byte baseline.
- [x] 5.9 Run cold-start, doctor, recovery, path/store safety, quickstart, wrapper CLI, Hook, fixture E2E, and isolated-install targeted tests; checkpoint exact results.

## 6. P0-4 Derived Prose Plus Exceptions

- [x] 6.1 Add an immutable delivery narrative facts read model over requirement/acceptance/task/qualification/execution/validation/failure-mode/finding/gate/trust/cycle/candidate IDs.
- [x] 6.2 Make `record_delivery()` derive and insert the exact active proven acceptance relation regardless of legacy acceptance prose.
- [x] 6.3 Derive authoritative validation, failure-mode coverage, gate review, finding, trust, cycle, and candidate narrative without depending on fixed prose literals.
- [x] 6.4 Derive a sorted changed-file list only from a valid comparable Git base and otherwise report `unknown/not derivable` without fabricating none.
- [x] 6.5 Render existing delivery prose fields only under `Legacy / Supplemental Notes` and ensure contradictory text cannot alter authority or readiness.
- [x] 6.6 Keep scope, rationale, unresolved/accepted risk, data/config exceptions, known gaps, and handoff as the only human-judgment narrative surfaces.
- [x] 6.7 Update delivery CLI help and generated workflow docs to mark compatibility prose flags supplemental while retaining their accepted syntax.
- [x] 6.8 Keep validation and quality-gate judgment prose distinct from execution evidence and derive their relation-backed projection fields.
- [x] 6.9 Turn all narrative red tests green, including scope-only links, contradictory prose, historical-cycle stability, and two byte-identical projection rebuilds.
- [x] 6.10 Prove schema 31 remains 30 product tables, 18 public schemas remain closed, and schema-27/28/29/30-to-31 migration creates no narrative authority.
- [x] 6.11 Run delivery, validation, gate, traceability, projection, schema, migration, historical audit, and candidate-identity targeted suites with exact accounting.

## 7. P0 Exit Gate

- [x] 7.1 Run all P0 targeted suites together with ResourceWarning as error and report pass/fail/skip/expected-failure separately.
- [x] 7.2 Run structure validation, all JSON parsing/schema checks, documentation contract, Skill eval, runtime smoke, fixture E2E, stability E2E, and `git diff --check`.
- [x] 7.3 Run complete unittest discovery and record exact counts without treating skip, blocked, not-run, fixture-only, or zero-test results as pass.
- [x] 7.4 Re-run 5k-fact, init/startup, DB size, plugin size, projection, status-byte, command-count, duplicated-prose-byte, and narrative-input metrics.
- [x] 7.5 Perform main-model adversarial review for hidden phase advancement, partial transaction, retry drift, fabricated verified receipt, concise-output omission, prose authority, and gate bypass.
- [x] 7.6 Resolve every P0 Critical/High/Medium finding, rerun its targeted tests plus the combined P0 gate, and record residual low risks.
- [x] 7.7 Checkpoint `LWL-P0-1` through `LWL-P0-4` closed with current evidence before any P1 production edit.

## 8. P1 Unified Entrypoint And Inventory Authority

- [x] 8.1 Add closed version-1 `references/distribution-manifest.json` for Skills, Hook files/events, templates, schemas, core, scripts, references, and public runtime domains.
- [x] 8.2 Refactor source plugin validation and `kafa doctor` to load the inspected plugin's manifest instead of module-level inventory copies.
- [x] 8.3 Refactor installed cache/app-server validation and structure validation to use the same inspected manifest with fail-closed missing/extra reporting.
- [x] 8.4 Refactor fixture/stability evaluators and install/release tests to use the manifest without allowing test-only inventory overrides in persistent evidence.
- [x] 8.5 Expose supported runtime domains through `kafa project ...` using manifest-derived passthrough while preserving specialized project doctor behavior.
- [x] 8.6 Update README, INSTALL, QUICKSTART, Skills, and generated command templates to teach only `kafa project ...` for ordinary projects and internal script paths only for maintainers.
- [x] 8.7 Add red/green tests for every supported project domain, argument/exit propagation, JSON cleanliness, missing runtime, and undeclared extra inventory.
- [x] 8.8 Verify exactly seven Skills, three Hooks, three templates, 18 schemas, approved runtime files, and unchanged local-only/Host ownership boundaries.

## 9. P1 Advanced-Mode And State-Visibility Reduction

- [x] 9.1 Change the entry Skill to use the five-field delegation packet by default and load the full delegation matrix only for parallel fan-out, shared files, or explicit advanced review.
- [x] 9.2 Add contract tests proving schema/migration/trust/gate/security/cross-module work still routes to root/deep ownership and cannot use the reduced packet to weaken review.
- [x] 9.3 Hide internal phase/cycle identifiers from default operator cards while preserving them in verbose/JSON and blocker diagnostics.
- [x] 9.4 Encode and generate closed triggers for audit, retrospective, live-host compatibility, and release rehearsal from the workflow contract.
- [x] 9.5 Update each advanced Skill to state its trigger and non-default status without deleting capability or weakening required evidence once triggered.
- [x] 9.6 Add Skill-eval scenarios for small single-producer work, parallel shared-file integration, repeated escapes, schema/runtime change, milestone review, and release-surface change.
- [x] 9.7 Measure default Skill plus reference bytes and verify entry Skill <=12,800 bytes and entry Skill plus required default references <=16,000 bytes.
- [x] 9.8 Run documentation, architecture, delegation, Skill eval, Host ownership, feature-freeze, structure, install, and fixture/stability targeted suites.

## 10. P1 Exit Gate

- [x] 10.1 Run all P1 entrypoint, inventory, delegation, state-card, trigger, documentation, install, and evaluator tests with exact accounting.
- [x] 10.2 Run complete unittest discovery, runtime smoke, Skill eval, fixture/stability E2E, structure/JSON/docs validation, and `git diff --check`.
- [x] 10.3 Re-run plugin/cache inventory, cold-start, status-byte, Skill-byte, parser-surface, and 5k-fact budgets and record justified deviations.
- [x] 10.4 Perform adversarial review for manifest self-tampering, missing inventory consumer, argument loss, hidden recovery state, under-triggered deep work, and advanced-check relabeling.
- [x] 10.5 Resolve every P1 Critical/High/Medium finding, rerun affected and combined gates, and close all `LWL-P1-*` items before P2 edits.

## 11. P2 Change-Scoped Evidence And Release Simplification

- [x] 11.1 Add a closed conservative change-scope classifier with explicit host, packaging, release-tooling, Native-evaluator, schema/runtime, docs-only, and unknown categories.
- [x] 11.2 Add red/green classifier tests proving every unknown path selects blocking real-Native evidence and docs-only scope cannot skip deterministic gates.
- [x] 11.3 Update release/validation workflow logic so real Native evidence is blocking only for declared scopes and unavailable selected evidence remains blocked/not-run.
- [x] 11.4 Extract one artifact subject/digest model shared by checksum, SBOM, provenance, isolated install, and rehearsal verification without removing any tamper check.
- [x] 11.5 Refactor supply-chain and rehearsal callers to the shared model and prove exact artifact/source digest mismatch still fails closed.
- [x] 11.6 Define a stable evidence-summary manifest with source/status/binary/scope/timing/digest/state and explicit volatile-detail retention class.
- [x] 11.7 Move newly generated volatile Native/rehearsal detail out of the default review surface where CI artifacts are available while preserving stable summaries and local opt-in detail.
- [x] 11.8 Reject missing, stale, digest-mismatched, fixture-substituted, or falsely current detail referenced by a stable summary.
- [x] 11.9 Change outcome reporting to one `field_metrics_status=not-observed` sentinel when no bounded field window exists and retain full metrics when observations exist.
- [x] 11.10 Update outcome, evidence, release, rehearsal, and operator documentation without deleting advanced capabilities or claiming a release.
- [x] 11.11 Run outcome, supply-chain tamper, rehearsal, workflow static, evidence-consistency, and scoped-classifier targeted suites.

## 12. P2 Exit Gate

- [x] 12.1 Build real wheel/sdist artifacts and run isolated venv/HOME install, discovery, cache digest, quickstart, doctor, Hook, uninstall, checksum, SBOM, and provenance verification.
- [x] 12.2 Run real Native single/parallel only if the locked change scope requires or current local audit explicitly selects it; otherwise record it advisory/not-run without fixture substitution.
- [x] 12.3 Run complete unittest discovery, runtime smoke, Skill eval, fixture/stability E2E, structure/JSON/docs validation, OpenSpec validation, and `git diff --check`.
- [x] 12.4 Re-run performance, artifact size, evidence diff size, outcome output size, and default-path token/byte metrics and compare against baseline.
- [x] 12.5 Perform adversarial review for scope-classifier evasion, unknown-path downgrade, summary-only false proof, removed tamper check, false field zero, and release capability loss.
- [x] 12.6 Resolve every P2 Critical/High/Medium finding, rerun affected and combined gates, and close all `LWL-P2-*` items.

## 13. Complete Acceptance, Independent QA, And Final Audit

- [x] 13.1 Run all delivery-plan, verified-patch, task, qualification, execution, trust, delivery, projection, schema, migration, rollback, path/store safety, CLI, docs, install, outcome, and release targeted suites.
- [x] 13.2 Run complete unittest discovery with exact pass/fail/skip/expected-failure counts and no fixture-only or not-run result counted as pass.
- [x] 13.3 Run runtime smoke, Skill eval, fixture E2E, stability E2E, structure validation, all JSON/schema checks, documentation generation/check, and `git diff --check`.
- [x] 13.4 Verify schema 31 and the 30-table inventory are unchanged and migration/rollback backups remain compatible and fail closed.
- [x] 13.5 Verify local-only runtime, root-controller single-writer, Native Host ownership, immutable execution, current-candidate verification, medium/high trust, and delivery prerequisite evaluator remain at least as strict.
- [x] 13.6 Verify no Connector/token/remote business API, `gh api` runtime, Host SDK worker, fabricated receipt, second lifecycle, release, deploy, production migration, or user installation replacement was introduced.
- [x] 13.7 Independent read-only QA A reviews workflow source, plan transaction/idempotence, concise output/recovery, schema/migration compatibility, and projection consistency.
- [x] 13.8 Independent read-only QA B reviews immutable verified-patch evidence, narrative authority, trust/gate non-bypass, advanced triggers, and release/evidence truthfulness.
- [x] 13.9 Main model fixes every QA Critical/High/Medium finding, reruns targeted/full gates, and obtains bounded re-review of each changed surface.
- [x] 13.10 Perform final four-angle adversarial review: logic gaps, incorrect facts, simpler alternatives, and proof sufficiency.
- [x] 13.11 Create a dated final audit with red/green mapping, exact counts, before/after command/token/byte/LOC/parser/inventory/performance metrics, artifact digests, and explicit not-run distinctions.
- [x] 13.12 Run final `openspec status --change local-workflow-lightweighting` and `openspec validate local-workflow-lightweighting` after every evidence update.
- [x] 13.13 Capture stable design, measured benefit, failure lessons, and operator guidance with `codex-brain capture`.
- [x] 13.14 Verify the worktree contains only intentional source/spec/audit changes and no generated cache/build residue.
- [x] 13.15 Report commit, push, merge, tag, release, deploy, production migration, and user-plugin replacement as not-run unless separately authorized.

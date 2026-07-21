## 1. Change Authority And Locked Baseline

- [x] 1.1 Read the applicable AGENTS.md, PROJECT_CONTEXT.md, project README, INSTALL, QUICKSTART, and retained Kafa Skill instructions before implementation.
- [x] 1.2 Confirm `main == origin/main == e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb` and preserve the existing untracked 2026-07-20 issue checklist.
- [x] 1.3 Query Codex Brain for relevant prior knowledge and record that no newer canonical delivery-integrity decision overrides the local audit.
- [x] 1.4 Run the solution-selection gate and lock one OpenSpec change with a single schema-31 migration in strict P0 -> P1 -> P2 order.
- [x] 1.5 Create and read the OpenSpec proposal instructions, then complete `proposal.md` from the confirmed issue checklist.
- [x] 1.6 Create and read the OpenSpec design/spec instructions, then complete `design.md` and the `local-delivery-kernel` delta spec.
- [x] 1.7 Validate the complete planning artifact set with `openspec status --change delivery-integrity-hardening` and `openspec validate delivery-integrity-hardening`.
- [x] 1.8 Freeze this `tasks.md` as the unique implementation checklist; do not add production scope without first updating proposal, design, spec, and tasks.

## 2. Baseline Metrics And P0 Red Contracts

- [x] 2.1 Record the exact pre-change source status, runtime/schema versions, active table inventory, public parser-node count, public schema count, plugin size, empty DB size, init time, 5k-fact mutation time, and full-suite count/time in a new baseline audit.
- [x] 2.2 Add deterministic API and CLI red tests for delivery with zero requirement, acceptance, baseline, task, confirmed scope, and readiness phase; assert no delivery row or cycle close.
- [x] 2.3 Add one-at-a-time minimum-graph red tests for missing requirement, missing acceptance, missing requirement-acceptance link, orphan acceptance, stale/missing baseline, unconfirmed scope, wrong phase, and missing accepted-task coverage.
- [x] 2.4 Add red tests proving `trace validate`, `validate --delivery`, and `record_delivery()` reject an acceptance whose only linked task is cancelled.
- [x] 2.5 Add red tests proving an unrelated cancelled task does not globally block a separate fully accepted graph and that accepted replacement coverage is sufficient.
- [x] 2.6 Add red tests proving target and acceptance existence alone cannot create acceptance evidence, including the expired-card acceptance plus arithmetic-test fixture.
- [x] 2.7 Add red tests for stale qualification after acceptance revision, command/kind/result-format/sandbox/result-path/stack/container-image target changes, and cross-acceptance reuse.
- [x] 2.8 Add red tests proving a passing gate that did not review the exact qualification cannot satisfy delivery and degraded review is not labelled independent.
- [x] 2.9 Add red tests comparing blocker codes across readiness entry, quickstart status, delivery validation, CLI recording, and direct API recording.
- [x] 2.10 Add red tests for entering readiness without circular phase requirements, record mode before readiness, delivered-cycle consistency, and atomic failure state.
- [x] 2.11 Run only the new P0 contract tests against the unmodified runtime and record every expected failure separately from setup errors.
- [x] 2.12 Checkpoint the red evidence in the baseline audit before any production runtime or schema edit.

## 3. Schema 31 And Recoverable Migration Foundation

- [x] 3.1 Add schema-31 red tests for exactly 30 approved product tables, declared SQLite internal tables, closed state checks, new immutable qualification rows, and extended immutable executions.
- [x] 3.2 Add schema-30-to-31 dry-run and real-migration red fixtures that preserve all local rows, create no qualification/outcome facts, and mark copied executions `legacy-incomplete`.
- [x] 3.3 Add supported schema-27/28/29-to-31 red fixtures and assert retired Connector/provider/dispatch facts remain excluded.
- [x] 3.4 Add migration preflight red tests for unknown requirement, acceptance, and failure-mode states plus deterministic `active` failure-mode normalization.
- [x] 3.5 Add injected pre-activation, post-activation doctor, projection publication, projection verification, DB restore, projection restore, and hard-exit recovery red tests for schema 31.
- [x] 3.6 Bump Kernel/runtime schema authority to 31 without changing package release state or pretending a release occurred.
- [x] 3.7 Define generation-neutral active schema/catalog constants while retaining explicit schema-27/28/29/30 source contracts.
- [x] 3.8 Implement schema-31 DDL for qualification, gate-qualification, and outcome-observation tables plus closed status/type constraints and execution provenance columns.
- [x] 3.9 Add insert-only triggers and foreign-key/index contracts for qualification facts and preserve execution/event immutability.
- [x] 3.10 Extend side-by-side migration for schema 30 and route schema 27/28/29 conversion directly to the same schema-31 target contract.
- [x] 3.11 Preserve exact legacy IDs/timestamps/facts, normalize only documented legacy states, add no synthetic qualification, and downgrade legacy execution provenance.
- [x] 3.12 Extend migration manifest, row-count checks, projection bundle, doctor, and domain validation for every schema-31 fact.
- [x] 3.13 Verify dry-run side-effect freedom and complete DB/projection rollback under every injected failure and cancellation boundary.
- [x] 3.14 Run schema lifecycle, migration, rollback, operation-lock, projection, doctor, and legacy compatibility targeted suites with ResourceWarning as error.

## 4. P0 Qualified Evidence And Task Coverage

- [x] 4.1 Implement one stable target-definition digest over all execution-relevant target fields, excluding timestamps and presentation-only data.
- [x] 4.2 Implement insert-only qualification creation with explicit ID, current acceptance revision, current target digest, rationale, actor, cycle, and timestamp.
- [x] 4.3 Add public `test-target qualify` CLI/API/proxy help and reject missing, blank, stale, duplicate-conflicting, or cross-cycle qualification inputs.
- [x] 4.4 Make `verify run --acceptance` resolve a current qualification before executing and bind the validation/execution evidence to its exact qualification.
- [x] 4.5 Keep verification without an acceptance available as audit execution evidence while making it ineligible for acceptance or delivery coverage.
- [x] 4.6 Add `gate record --qualification` repeatable links and validate that each link belongs to the current cycle, candidate-relevant evidence, and reviewed mapping.
- [x] 4.7 Make delivery require the current gate -> qualification -> validation -> execution -> target-digest -> acceptance-revision join.
- [x] 4.8 Update acceptance/target mutations so digest or revision changes make dependent qualification and validation stale without rewriting history.
- [x] 4.9 Update quickstart to create a clearly labelled procedural user-input qualification and still stop before independent review.
- [x] 4.10 Change traceability so only accepted tasks satisfy completed coverage; cancelled tasks remain visible but never count as accepted.
- [x] 4.11 Make task cancellation remove sole delivery coverage without globally blocking acceptances that have another accepted task.
- [x] 4.12 Add qualification projection/schema output showing acceptance revision, target digest, rationale, actor, and exact gate review status.
- [x] 4.13 Turn every P0 qualification/cancelled-task red scenario green and verify no test relies on natural-language inference.
- [x] 4.14 Document the residual boundary: qualification is auditable procedural accountability, not automatic semantic proof or cryptographic provenance.

## 5. P0 Canonical Prerequisite Evaluator And Public Journey

- [x] 5.1 Introduce structured `DeliveryBlocker` facts with stable codes, messages, and entity identity.
- [x] 5.2 Implement pure read-only `enter-readiness`, `record-delivery`, and `delivered-consistency` evaluator modes.
- [x] 5.3 Enforce at least one active requirement, closed requirement-acceptance graph, no orphan active acceptance, current frozen confirmed baseline, and accepted-task coverage.
- [x] 5.4 Enforce qualified current-candidate passing immutable execution, current risk state, exact gate-reviewed qualification, and current latest gate.
- [x] 5.5 Reuse the evaluator in internal readiness transition, quickstart status, `validate --delivery`, CLI record, and direct `record_delivery()`; remove or implement the ignored phase flag.
- [x] 5.6 Add `baseline confirm` under the existing baseline domain to freeze the exact baseline and explicitly confirm scope atomically.
- [x] 5.7 Preserve plain `baseline freeze` as a snapshot operation that does not confirm scope.
- [x] 5.8 Add `delivery ready` under the existing delivery domain and atomically enter readiness only after `enter-readiness` returns no blockers.
- [x] 5.9 Require exact readiness phase and active cycle in record mode, then verify delivery/cycle/candidate/phase consistency after recording.
- [x] 5.10 Preserve candidate-change checks before insert and before commit and prove failed recording leaves no delivery row or cycle close.
- [x] 5.11 Update quickstart next-actions and the full manual journey to use baseline confirmation, qualification review, readiness, and record in legal order.
- [x] 5.12 Turn every P0 minimum-graph/shared-surface/public-journey red scenario green with stable blocker assertions.
- [x] 5.13 Run delivery policy, delivery cycles, traceability, single-writer task, quickstart, CLI, projection, and schema-31 targeted suites.
- [x] 5.14 Checkpoint P0 source, red/green evidence, exact test counts, migration behavior, and remaining risks before starting P1.
- [x] 5.15 Make accepted-task coverage require non-empty evidence plus an accept actor/event in current and historical delivery evaluation.
- [x] 5.16 Bind historical cycle audit to persisted candidate, delivery-time trust, legal baseline revision, ordered confirmation/gate/delivery events, cycle event digest, and cycle-scoped invariants.
- [x] 5.17 Preserve decision writes on supported legacy schemas that expose the decision authority (27, 29, and 30), including their generation-specific audit event format, until migration snapshots the final committed state.
- [x] 5.18 Recover a legacy finding candidate only from one coherent evidence/gate scope; reject conflicting candidate provenance or cross-cycle gate links before activation, and keep deterministic fixture blocker accounting aligned with stable delivery codes.

## 6. P0 Stop-Ship Exit Gate

- [x] 6.1 Re-run all P0 tests with ResourceWarning as error and record exact pass/fail counts with zero expected-failure disguises.
- [x] 6.2 Run runtime smoke, structure validation, all public JSON parsing, documentation contract, and `git diff --check`.
- [x] 6.3 Run fixture and stability E2E and verify the four former false-delivery scenarios now fail closed.
- [x] 6.4 Run schema-30 real-copy dry-run, migration, injected failure, rollback, and post-migration public journey in an isolated temporary HOME.
- [x] 6.5 Recompute P0 outcome benchmark: the same four before scenarios must produce four blocked after results without fixture/field-evidence confusion.
- [x] 6.6 Perform a main-model adversarial review for graph gaps, direct-API bypass, qualification rubber stamping, cancellation semantics, circular readiness, data loss, and stale candidate.
- [x] 6.7 Resolve every P0 Critical/High/Medium finding and rerun the affected checkpoint.
- [x] 6.8 Mark KAFA-P0-1 through KAFA-P0-4 closed in the issue checklist only after all evidence above exists.

## 7. P1 Medium Risk And Closed State/Schema Contracts

- [x] 7.1 Add red tests for uncovered medium failure mode, open medium finding, incomplete/expired/stale acceptance, and empty degraded residual-risk notes.
- [x] 7.2 Add positive tests for qualified structured medium coverage, complete current accepted/exempt metadata, low-risk degraded review, and unchanged high/critical strictness.
- [x] 7.3 Expand delivery risk evaluation so identified medium failure modes require qualified structured current-candidate coverage or complete accepted/exempt metadata.
- [x] 7.4 Make open medium findings block unless resolved, false-positive, or completely accepted for the current candidate/revision and unexpired window.
- [x] 7.5 Require non-empty explicit residual-risk text for same-context-degraded low/medium gates and label medium acceptance as procedural accepted-risk.
- [x] 7.6 Prove medium acceptance cannot waive graph, qualification, accepted-task, candidate, execution-provenance, or gate prerequisites.
- [x] 7.7 Define canonical requirement `active/cancelled`, acceptance `active/cancelled`, and failure-mode `identified/accepted/exempt` sets in guard, CLI, DDL, doctor, migration, projections, and docs.
- [x] 7.8 Reject `requirement --status nonsense` before mutation and add supported acceptance cancellation/state mutation only if required by the locked spec.
- [x] 7.9 Add unique `urn:kafa:schema:31:<entity>` IDs and explicit `additionalProperties` to every shipped JSON schema, including qualification and outcome schemas.
- [x] 7.10 Extend the closed runtime schema subset for minimum, minLength, const, and pattern and reject unsupported shipped keywords during structure validation.
- [x] 7.11 Add schema fixture tests for every enum, numeric/string constraint, unknown field policy, unique ID, and unsupported keyword.
- [x] 7.12 Update schema-31 migration tests for invalid legacy states, exact normalization, and no silent coercion.
- [x] 7.13 Update invariant checker, doctor, projections, and static architecture tests to the same state/schema authority.
- [x] 7.14 Run medium policy, trust, schema contract, doctor, migration, and projection targeted suites with warnings as errors.
- [x] 7.15 Checkpoint P1 risk/state/schema evidence and unresolved findings before provenance work.

## 8. P1 Execution Provenance And Manual Workflow Closure

- [x] 8.1 Add red tests for missing target digest, local platform/runtime facts, runtime executable digest, policy version, engine version, and resolved container image identity.
- [x] 8.2 Add red tests for missing local container image, mutable image drift, engine/image change before commit, and implicit-pull prohibition.
- [x] 8.3 Extend immutable CommandResult/execution rows and JSON schema with the locked schema-31 provenance fields and exact type/status constraints.
- [x] 8.4 Capture local platform, runtime executable/version/digest, policy version, and target-definition digest before execution becomes eligible.
- [x] 8.5 Resolve an already-local Docker/Podman image to immutable identity, record engine version and requested/resolved image, run by immutable identity, and never pull implicitly.
- [x] 8.6 Recheck target/runtime/container provenance at commit boundaries and reject stale results without passing validation.
- [x] 8.7 Require supported structured results for medium/high/critical unit or integration failure-mode coverage while preserving documented low-risk regex paths.
- [x] 8.8 Keep schema-30 and older executions historical with `legacy-incomplete` provenance and prevent them from satisfying schema-31 current delivery.
- [x] 8.9 Add a real executable E2E for the documented non-quickstart baseline-confirm -> qualify -> verify -> accept -> gate -> ready -> record journey.
- [x] 8.10 Update README, INSTALL, QUICKSTART, examples, retained Skills, templates, proxy help, and changelog for schema 31 and the legal public journey.
- [x] 8.11 Verify docs do not restore generic phase/Host lifecycle commands or introduce Connector/network trust claims.
- [x] 8.12 Run execution policy, structured result, container mock/real-capability, CLI help, docs, and full public-journey targeted suites.

## 9. P1 Exit Gate

- [x] 9.1 Run all P1 medium/state/schema/provenance/public-journey tests with ResourceWarning as error and record exact counts.
- [x] 9.2 Run runtime smoke, Skill eval, fixture/stability E2E, schema-31 migration matrix, structure/JSON/docs validation, and `git diff --check`.
- [x] 9.3 Run the 5k-fact benchmark and keep mutation <=0.050s plus existing init, DB, plugin, and startup budgets or record a user-visible justified deviation.
- [x] 9.4 Verify schema 31 remains local-only, root-single-writer, and free of retired Connector/provider/dispatch/Host worker surfaces.
- [x] 9.5 Perform adversarial review for medium-risk bypass, state coercion, thin-schema claims, mutable container identity, implicit network pulls, and public/internal workflow divergence.
- [x] 9.6 Resolve every P1 Critical/High/Medium finding and rerun affected checkpoints.
- [x] 9.7 Mark KAFA-P1-1 through KAFA-P1-4 closed only from current evidence.

## 10. P2 Outcome, Current Evidence, And Supply Chain

- [x] 10.1 Add schema/API/CLI tests for bounded local outcome observations and reject unknown kind, negative value, blank details/actor, invalid cycle, or malformed timestamp.
- [x] 10.2 Implement `cycle outcome-record` and `cycle outcome-report --json` under root-single-writer/local-only boundaries.
- [x] 10.3 Define versioned numerator, denominator, window, missing-data, regression-vs-field semantics for false-green, escaped defect, rework, recovery, delivery time, and qualification coverage.
- [x] 10.4 Add a deterministic before/after P0 outcome benchmark and report insufficient-data/not-run honestly for unavailable field windows.
- [x] 10.5 Add a complete root MIT LICENSE and validate package, README, wheel/sdist, and repository license metadata alignment.
- [x] 10.6 Check official primary documentation for current SBOM and GitHub build-provenance tooling, then pin the selected build-only versions/actions.
- [x] 10.7 Implement standard wheel/sdist SBOM generation plus source/artifact provenance statements with exact SHA-256 binding.
- [x] 10.8 Implement tamper verification for checksum, SBOM subject, provenance subject, source identity, and artifact bytes.
- [x] 10.9 Add a no-publish release rehearsal that builds exact artifacts, installs them in isolated venv/HOME, verifies plugin discovery/cache/quickstart/doctor/hook/uninstall, and verifies supply-chain artifacts.
- [x] 10.10 Update release workflow to generate/check SBOM and official build provenance before an explicitly authorized publish job, without triggering it now.
- [x] 10.11 Mark committed old dirty-head Native reports as historical before generating replacement current evidence.
- [x] 10.12 Run real Native Codex single and parallel synthetic profiles on the stabilized exact candidate and validate source/status/binary/token/scope/timing facts.
- [x] 10.13 Persist only clean exact-candidate reports; keep blocked/failed/skipped/not-run separate from fixture results.
- [x] 10.14 Verify existing GitHub required check contexts and configure reversible main protection/ruleset for pull-request review plus supported Ubuntu/macOS/Windows checks.
- [x] 10.15 Read back branch protection/ruleset through GitHub API and record the exact response; do not commit, push, tag, release, or deploy without separate authorization.
- [x] 10.16 Run outcome, release-rehearsal, install, Native report, workflow static, documentation, and governance targeted checks.
- [x] 10.17 Mark KAFA-P2-1 through KAFA-P2-3 closed only after the current evidence and governance response exist.

## 11. Complete Local Acceptance And Performance Evidence

- [x] 11.1 Run all delivery, qualification, task, risk, execution, schema, migration, rollback, projection, path/store safety, CLI, docs, install, and stop-ship targeted suites with ResourceWarning as error.
- [x] 11.2 Run complete unittest discovery; report exact pass/fail/skip/expected-failure counts and do not count skips as passes.
- [x] 11.3 Run runtime smoke, Skill eval, fixture E2E, stability E2E, and every deterministic regression outcome profile.
- [x] 11.4 Validate Plugin structure, all JSON files, all shipped schema keywords/IDs, OpenSpec change, canonical local-only boundaries, and `git diff --check`.
- [x] 11.5 Build real wheel and source artifacts and complete isolated install, discovery, cache digest, quickstart, migration, doctor, hook, uninstall, SBOM, provenance, and checksum evidence.
- [x] 11.6 Re-run 5k-fact benchmark, empty init/startup, DB size, plugin size, projection rebuild, and delivery-evaluator timing budgets.
- [x] 11.7 Compare before/after LOC, table/schema/parser surface, artifact size, timing, token, qualification coverage, and false-green-prevention metrics without hiding accepted deviations.
- [x] 11.8 Verify user-level Kafa/plugin installation remains unchanged and no business project database was migrated.
- [x] 11.9 Confirm no external Connector, token, `gh api` business runtime, Host SDK worker, fabricated receipt, or second task lifecycle was introduced.
- [x] 11.10 Confirm no tag, release, deployment, production migration, secret change, paid resource, or user installation replacement occurred.
- [x] 11.11 Re-run `openspec status --change delivery-integrity-hardening` and `openspec validate delivery-integrity-hardening` after all implementation evidence updates.
- [x] 11.12 Checkpoint the exact final local candidate identity and every command/result needed for independent QA.

## 12. Independent QA, Final Audit, And Archive

- [x] 12.1 Independent read-only QA A reviews minimum graph, qualification, cancelled coverage, schema-31 migration, backup, rollback, and projection coherence.
- [x] 12.2 Independent read-only QA B reviews medium/high trust, shared readiness modes, execution provenance, public journey, outcome truthfulness, and supply-chain claims.
- [x] 12.3 Main model fixes every QA Critical/High/Medium finding, reruns affected targeted/full gates, and requests one bounded re-review.
- [x] 12.4 Perform final adversarial review from logic gaps, incorrect facts, simpler alternatives, and proof sufficiency, including direct API and corrupted-DB attempts.
- [x] 12.5 Update the 2026-07-20 issue checklist with exact closure evidence for every P0/P1/P2 item and preserve any residual limitation.
- [x] 12.6 Create a final dated audit with red/green mapping, migration/rollback evidence, exact test counts, Native evidence, supply-chain outputs, governance response, before/after metrics, and not-run distinctions.
- [x] 12.7 Update canonical purpose/version language and archive the completed change into `openspec/specs/local-delivery-kernel/spec.md` only after every task is evidenced.
- [x] 12.8 Validate the archive, documentation contract, schema inventory, release metadata, JSON, exact source status, and whitespace after archival edits.
- [x] 12.9 Capture stable outcome, failure lessons, migration behavior, and operational guidance in Codex Brain.
- [x] 12.10 Verify the working tree contains only intentional implementation/audit changes and generated caches/artifacts are absent or ignored.
- [x] 12.11 Report commit/push/merge/remote-CI as not-run unless separately authorized; do not infer authorization from local completion.
- [x] 12.12 Complete the active goal only when every checkbox and requirement has authoritative current evidence and no required work remains.

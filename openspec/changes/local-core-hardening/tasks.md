## 1. Contract and red evidence

- [x] 1.1 Main model: validate `local-core-slimming`, record the current executable-source identity, and map every new scenario to a deterministic test before production edits.
- [x] 1.2 Main model: add multiprocessing red tests proving an already-active writer is included and new read/write operations fail after migration announcement without timing sleeps.
- [x] 1.3 Main model: add red tests for final-doctor and partial-projection rollback, exact byte restoration, removal of newly created views, and `rollback-incomplete` metadata.
- [x] 1.4 Main model: add red integration tests proving `same-context-degraded` plus distinct-looking IDs and complete risk acceptance remains `human-review-required`, while the reviewed-local path stays valid.
- [x] 1.5 Checkpoint: run only the new tests, capture the three expected failure families, and confirm existing positive migration/trust tests still pass.

## 2. Project operation lock

- [x] 2.1 Main model: implement the cross-platform same-thread-reentrant operation lock in `core/store.py` with five-second timeout and no InMemoryStore behavior change.
- [x] 2.2 Main model: make file-backed connection, transaction, and backup contexts hold the operation lock until SQLite handles close.
- [x] 2.3 Main model: make local-core migration announce with the existing sentinel, acquire the same operation lock before reading the source, hold it through rollback, and permit only owner-thread callback reentry.
- [x] 2.4 Main model: make normal runtime and `kafa project doctor` fail closed on the migration sentinel before opening SQLite, including actionable stale-sentinel output.
- [x] 2.5 Checkpoint: run store, concurrency, migration, admin-read, ResourceWarning, and 5k-mutation tests; inspect lock/sentinel cleanup after success and injected failure.

## 3. Projection-coherent rollback

- [x] 3.1 Main model: define the exact ordered projection path inventory without adding a projection or public CLI command.
- [x] 3.2 Main model: create and verify the bounded projection backup beside the SQLite backup and record safe manifest metadata without project secrets.
- [x] 3.3 Main model: run database doctor before live projection publication, then atomically restore and verify all original projections after any post-activation failure.
- [x] 3.4 Main model: fail closed with `rollback-incomplete` and both error chains when projection restoration cannot complete.
- [x] 3.5 Checkpoint: run schema 27/28/29 migration, failure injection, doctor, projection, manifest, and backup recovery tests with ResourceWarning promoted to error.

## 4. High-risk review enforcement

- [x] 4.1 Main model: add required `review_status` to `evaluate_local_trust` and update every production/test caller explicitly.
- [x] 4.2 Main model: require `reviewed-local` plus distinct non-empty contexts for high/critical accepted-risk; preserve low/medium degraded behavior.
- [x] 4.3 Main model: add direct and CLI integration regressions for degraded spoofing, reviewed-local success, incomplete acceptance, and stale review metadata.
- [x] 4.4 Checkpoint: run local delivery policy, stop-ship, cycle, execution, finding, dirty-tree, and high-risk negative suites.

## 5. Full validation and installation

- [x] 5.1 Main model: run py_compile, structure validation, JSON validation, OpenSpec validation, and `git diff --check`.
- [x] 5.2 Main model: run the complete ResourceWarning-as-error unittest suite; skipped, blocked, not-run, or fixture-only results are not passes.
- [x] 5.3 Main model: run runtime smoke, Skill eval, fixture/stability E2E, migration/rollback matrix, and the local benchmark; confirm the mutation budget remains <=0.050 seconds.
- [x] 5.4 Main model: rerun real Native Codex single and parallel profiles, update persistent reports, and independently recompute source/binary/scope/token/timing consistency.
- [x] 5.5 Main model: build and test real wheel/source artifacts in an isolated venv and HOME; do not replace the active user installation.
- [x] 5.6 Mechanical after contracts are green: add an explicit migration/trust hardening step to all three validation OS jobs; main model reviews the exact workflow diff.
- [x] 5.7 Authorization gate: remote Ubuntu/macOS/Windows CI remains `not-run` unless the user separately authorizes commit and push; never infer permission to tag, release, or deploy.

## 6. Independent QA and final audit

- [x] 6.1 Independent migration QA: review operation-lock ordering, Windows handles, backup/restore, failure injection, and data preservation with exact commands and residual risks.
- [x] 6.2 Independent trust QA: review review-status propagation, accepted-risk semantics, immutable evidence, Native Host ownership, and external-runtime absence.
- [x] 6.3 Main model: remediate every critical/high finding and any unaccepted medium finding, then rerun affected tests and both reviews.
- [x] 6.4 Main model: update the final audit with superseded findings, red/green evidence, before/after performance, installation truth, live status, remote-CI status, and remaining limitations.
- [x] 6.5 Final checkpoint: confirm every `local-core-hardening` task is checked, both OpenSpec changes validate, the worktree contains only planned/user changes, and no commit, push, merge, tag, release, deploy, or active-user installation occurred.

## 7. Publication QA revision integrity follow-up

- [x] 7.1 Main model: add deterministic schema-27/28/29 staging red tests proving fractional, textual, zero, and negative project/gate revisions cannot be coerced into current schema-30 trust metadata.
- [x] 7.2 Main model: add a red delivery integration proving an accepted high/critical finding still requires `reviewed-local` and cannot pass through `same-context-degraded`.
- [x] 7.3 Main model: add a red documentation contract proving risk acceptance cannot waive structured execution, exact `reviewed-local`, or distinct non-empty contexts.
- [x] 7.4 Main model: add a red migration test proving fractional command-result and policy flags cannot become immutable execution evidence.
- [x] 7.5 Main model: replace migration trust coercion with exact SQLite-integer validation, include remaining high/critical findings in trust evaluation, align packaged instructions, and leave no activatable staging database on revision rejection.
- [x] 7.6 Main model: add a red active-runtime regression proving fractional execution results and policy flags cannot pass delivery evaluation after direct SQLite tampering.
- [x] 7.7 Main model: make schema-30 delivery evaluation require exact SQLite integers and flags for all gateable execution metadata.
- [x] 7.8 Checkpoint: rerun migration/trust targeted suites, complete strict regression, Native identity checks, artifact install, and both independent reviews against the fixed workspace.
- [x] 7.9 Publication gate: record the final audit evidence and retain the PR rule that the new pushed HEAD's complete Ubuntu/macOS/Windows matrix is required before merge.

## 8. Windows isolated-install handle follow-up

- [x] 8.1 Main model: preserve the real Windows `WinError 32` log as red evidence and add a deterministic regression requiring the quickstart database reader to close its connection.
- [x] 8.2 Main model: explicitly close the isolated-install SQLite reader without changing user installation scope or plugin behavior.
- [ ] 8.3 Checkpoint: rerun install/release tests, full regression, Native reports, artifact smoke, OpenSpec validation, fresh QA, and the complete new three-platform CI matrix.

## 9. Windows sentinel path-alias follow-up

- [x] 9.1 Main model: preserve both `RUNNER~1` versus `runneradmin` CI failures and make the project-doctor regression compare the resolved sentinel path while retaining PID and operator-remediation assertions.
- [ ] 9.2 Checkpoint: rerun install/release and full strict regression, update final audit evidence, and require both new three-platform matrices to pass before closing 8.3 or merging.

## 10. Windows full-regression portability follow-up

- [x] 10.1 Main model: preserve both four-failure Windows logs and map projection manifest separators, foreign Native binary paths, CRLF executable identity, and Skill host-command quoting to deterministic regressions.
- [x] 10.2 Main model: keep projection manifest keys POSIX, enforce LF checkout and hash actual runtime bytes plus tracked mode for cross-platform eval identity, accept POSIX or Windows absolute Native path syntax without waiving current-binary checks, and use platform-correct shell quoting in Skill tests.
- [ ] 10.3 Checkpoint: rerun affected modules, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 11. Post-activation cancellation rollback follow-up

- [x] 11.1 Main model: preserve the independent-QA reproduction and add deterministic red tests proving `KeyboardInterrupt`, `SystemExit`, and async cancellation after activation cannot leave schema 30 active with schema 29 projections.
- [x] 11.2 Main model: make the activation transaction catch cancellation-class failures, restore and verify both database and projections, preserve the original exception type after successful rollback, and record rollback failures without swallowing cancellation.
- [ ] 11.3 Checkpoint: rerun migration/recovery targeted tests, all affected modules, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 12. Native source-identity filter-injection follow-up

- [x] 12.1 Main model: preserve the independent-QA clean-filter bypass and add a deterministic red test proving local Git attributes cannot execute a filter or collapse semantically different executable bytes to one identity.
- [x] 12.2 Main model: remove every executable Git-filter path from source identity, enforce repository text checkout as LF, hash actual runtime bytes plus tracked file mode, and retain path/add/delete/status binding.
- [ ] 12.3 Checkpoint: rerun source-identity and report-forgery tests, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 13. Native source-identity framing follow-up

- [x] 13.1 Main model: preserve the independent-QA NUL-framing collision and add a deterministic red test proving one file cannot impersonate a second path/mode/content record inside the outer workspace digest.
- [x] 13.2 Main model: hash each file's exact runtime bytes to a fixed-width SHA-256 before composing the path/mode workspace identity, without weakening add/delete, mode, binary, or source-scope binding.
- [ ] 13.3 Checkpoint: rerun source-identity and report-forgery tests, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 14. Rollback-incomplete fail-closed follow-up

- [x] 14.1 Main model: preserve the independent-QA database-restore cancellation reproduction and add a deterministic red test proving a missing active DB cannot be recreated by a normal Store after rollback becomes incomplete.
- [x] 14.2 Main model: retain an actionable recovery-required migration sentinel for every rollback-incomplete outcome while continuing to remove it after verified complete rollback.
- [ ] 14.3 Checkpoint: rerun migration/recovery and sentinel tests, all affected modules, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 15. Rollback diagnostic cancellation follow-up

- [x] 15.1 Main model: add a deterministic red test proving an empty-message cancellation during failed-schema30 diagnostic preservation remains visible after DB/projection authority is restored.
- [x] 15.2 Main model: serialize cancellation-class failures with a non-empty type-aware diagnostic throughout failed-schema30 digest, move, copy, cleanup, and verification handling.
- [ ] 15.3 Checkpoint: rerun migration/recovery and manifest diagnostics, all affected modules, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 16. Native source enumeration and status follow-up

- [x] 16.1 Main model: add deterministic red tests proving status calculation cannot execute a clean filter and ignored-but-runtime-importable source cannot disappear from workspace identity.
- [x] 16.2 Main model: enumerate actual scoped filesystem sources including ignored files and derive source status from raw runtime bytes versus index metadata without calling `git status`, conversion filters, or exclude rules.
- [ ] 16.3 Checkpoint: rerun clean-filter, ignored-source, source-identity, and report-forgery tests, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 17. State-driven migration recovery guard follow-up

- [x] 17.1 Main model: add deterministic red tests for a rollback-incomplete manifest-write failure and a second cancellation immediately after schema-30 diagnostic preservation.
- [x] 17.2 Main model: enter recovery-required state before atomic activation and retain the sentinel across every recovery interruption, clearing it only after verified rollback plus terminal manifest or normal migration completion.
- [ ] 17.3 Checkpoint: rerun migration/recovery, dual-failure, sentinel, and manifest tests, all affected modules, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 18. Recovery-required doctor diagnostics follow-up

- [x] 18.1 Main model: add a red project-doctor regression requiring rollback-incomplete status, manifest path, and recovery-specific operator guidance without opening SQLite.
- [x] 18.2 Main model: align Store and `kafa project doctor` sentinel diagnostics so recovery-required state cannot be mistaken for a safely removable stale migration lock.
- [ ] 18.3 Checkpoint: rerun project-doctor, Store sentinel, install/release, migration/recovery, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 19. Native SHA-256 and executable-mode follow-up

- [x] 19.1 Main model: add deterministic red tests proving the workspace trust digest uses fixed per-file SHA-256 and a POSIX runtime chmod cannot hide behind unchanged index mode.
- [x] 19.2 Main model: separate fixed runtime SHA-256 from Git object IDs and bind actual POSIX executable mode while retaining canonical index mode on Windows.
- [ ] 19.3 Checkpoint: rerun source-identity, filter/fsmonitor, ignored-source, mode, framing, and report-forgery tests, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 20. Native symlink and gitlink fail-closed follow-up

- [x] 20.1 Main model: add deterministic red tests proving same-byte source symlinks and gitlink/submodule entries cannot preserve a valid workspace identity.
- [x] 20.2 Main model: fail source identity closed on actual symlinks, non-regular scoped paths, and tracked modes outside regular `100644`/`100755` files.
- [ ] 20.3 Checkpoint: rerun symlink/gitlink, source-identity, report-forgery, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 21. Native Git environment isolation follow-up

- [x] 21.1 Main model: add deterministic red tests proving ambient `GIT_WORK_TREE` cannot hide runtime source and every object-read command disables lazy fetching.
- [x] 21.2 Main model: clear inherited `GIT_*` variables for source identity, disable fsmonitor/lazy fetch/prompting, and fail closed on unavailable local Git objects without invoking remote helpers.
- [ ] 21.3 Checkpoint: rerun Git-environment, filter/fsmonitor, ignored-source, symlink/gitlink, source-identity, real Native profiles, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 22. Native verification-evidence consistency follow-up

- [x] 22.1 Main model: preserve the independent-QA false-pass reproduction and add deterministic red tests proving a persisted passing single or parallel Native report cannot survive failed controller verification, zero immutable execution/validation counts, incomplete task progression, or a present retired Host surface.
- [x] 22.2 Main model: make report consistency recompute every passing live scenario's controller verification, task, immutable execution/validation, integration dependency, and retired Host surface contract so `should_fail` rejects any mismatch.
- [ ] 22.3 Checkpoint: rerun report-tampering, Native single/parallel, source-identity, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 23. Production candidate-identity hardening follow-up

- [x] 23.1 Main model: preserve the independent-QA delivery bypasses and add deterministic red tests for ignored runtime source drift, executable-mode changes, ambiguous file framing, same-byte symlinks, ambient Git overrides, and unavailable local Git objects against the real schema-30 delivery gate.
- [x] 23.2 Main model: make production Git/content candidate identity use isolated Git commands, actual runtime bytes, fixed per-file SHA-256 framing, executable modes, ignored runtime files, and fail-closed symlink/gitlink/object semantics without including Kafa-owned state.
- [ ] 23.3 Checkpoint: rerun candidate-identity, dirty-tree, immutable execution, trust, Native identity, performance, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 24. Hard-process-exit migration recovery follow-up

- [x] 24.1 Main model: preserve the independent-QA hard-exit reproduction and add a deterministic process test proving exit after atomic activation leaves a durable recovery-required sentinel with manifest guidance before any schema-30/projection split can be mistaken for a removable stale lock.
- [x] 24.2 Main model: atomically persist and fsync recovery-required sentinel metadata before database replacement, retain it across hard exit or metadata-refresh failure, and make Store/project-doctor distinguish it from an ordinary pre-activation stale sentinel.
- [ ] 24.3 Checkpoint: rerun hard-exit, cancellation, rollback-incomplete, sentinel, migration/recovery, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 25. Recovery contract and operator-guidance alignment

- [x] 25.1 Main model: correct the hardening spec, design, README, INSTALL, and final audit so only successful migration or verified complete rollback clears the sentinel, while rollback-incomplete, hard exit, and interrupted recovery retain a durable recovery-required sentinel.
- [x] 25.2 Main model: add documentation-contract coverage for the operator distinction and rerun strict OpenSpec plus public documentation validation.
- [x] 25.3 Checkpoint: reconcile every migration success/failure statement with runtime tests and fresh migration QA before closure.

## 26. Mandatory projection activation validation follow-up

- [x] 26.1 Main model: preserve the independent-QA direct-core success bypass and add a red test proving migration cannot report schema 30 activated while the projection activation validator is absent or old views remain live.
- [x] 26.2 Main model: make projection publication/verification a mandatory success precondition for every production migration caller, while preserving test-only failure injection and complete rollback semantics.
- [ ] 26.3 Checkpoint: rerun direct-core/public-CLI migration success, projection publication/verification, rollback, Native migration probe, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 27. Legacy migration invariant normalization follow-up

- [x] 27.1 Main model: preserve the mandatory-validator failure and add red tests proving migrated executions use a schema-30 sandbox status and retired invalidation source types cannot enter the active database.
- [x] 27.2 Main model: normalize an unrequested sandbox to the canonical empty status and retain only schema-30-supported invalidation source/target pairs, leaving rejected legacy rows in the verified backup.
- [ ] 27.3 Checkpoint: rerun schema 27/28/29 staging, public migration projection validation, stability E2E, complete strict regression, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 28. Candidate source versus dependency-environment follow-up

- [x] 28.1 Main model: preserve the documented `.venv/bin/python` production failure and add deterministic Git/no-Git red tests proving bounded dependency environments and generated tool caches do not invalidate candidate identity while ignored runtime source, lockfiles, adjacent prefixes, and versioned dependency-named roots remain bound.
- [x] 28.2 Main model: exclude only the exact non-versioned top-level `.venv`, `venv`, `.tox`, `.nox`, and `node_modules` roots plus generated tool caches through the shared Git/content source filter, and align the OpenSpec/public contract without weakening symlink or ignored-source fail-closed behavior.
- [ ] 28.3 Checkpoint: rerun candidate-identity, delivery, Native identity, performance, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 29. Native report profile and telemetry integrity follow-up

- [x] 29.1 Main model: preserve fresh-QA reproductions and add deterministic red tests for unknown/Connector report modes, forged fixture inventory/category, zero or non-finite telemetry, substituted Native binary, and parallel producer task/scope permutation.
- [x] 29.2 Main model: enforce the exact mode/evidence/matrix/scenario contracts, positive finite timing/token evidence, current Native binary/version at generation-time validation, and immutable parallel task-to-scope/context/target/acceptance mapping.
- [ ] 29.3 Checkpoint: rerun all report-forgery, fake/live Native, fixture/stability, documentation, full regression, artifact, fresh trust QA, and both complete three-platform matrices before merge.

## 30. Exact generated-source exclusion follow-up

- [x] 30.1 Main model: preserve fresh-QA clean-commit `.gitignore`/reserved-sibling bypasses and no-Git FIFO omission, then add deterministic red tests with exact generated-path controls.
- [x] 30.2 Main model: replace broad `.gitignore`, `.codex/agents/`, and `docs/harness/` exclusions with exact generated template/projection paths and make no-Git non-regular paths fail closed.
- [ ] 30.3 Checkpoint: rerun Git/content candidate identity, generated projection stability, delivery trust, Native identity, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 31. Native evidence source and persistence follow-up

- [x] 31.1 Main model: preserve fresh-QA unmerged-index, explicit-test-binary persistence, and semantic-extra reproductions; add deterministic red tests for all three.
- [x] 31.2 Main model: invalidate scoped unmerged Native source, close and version the passing report/producer schema, and refuse `--evidence-out` for passing reports created with an explicit test binary override.
- [ ] 31.3 Checkpoint: rerun source identity, fake/real Native, compact persistence, documentation, full strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 32. Legacy revision pre-conversion validation follow-up

- [x] 32.1 Main model: preserve the final migration-QA reproduction and add the complete schema 27/28 project/gate fractional, textual, zero, and negative revision red matrix.
- [x] 32.2 Main model: reject every non-positive or non-integer project/gate trust revision before isolated legacy conversion can apply SQLite arithmetic, without leaving an activatable staging database.
- [ ] 32.3 Checkpoint: rerun legacy staging, complete migration/recovery, strict regression, real Native profiles, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 33. Native matrix generation-fact follow-up

- [x] 33.1 Main model: preserve the final trust-QA matrix-tampering reproduction and add red tests for forged platform, Python, Git, and container metadata while retaining cross-platform historical validation.
- [x] 33.2 Main model: validate matrix field types on every report and bind generation-time validation to the current platform, Python, Git, and container facts without making persisted foreign-platform evidence self-invalidating.
- [ ] 33.3 Checkpoint: rerun report forgery, fake/real Native, persistent documentation, strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 34. HEAD-only gitlink fail-closed follow-up

- [x] 34.1 Main model: preserve the final trust-QA HEAD-only gitlink reproduction and add deterministic red tests against both production candidate identity and Native evaluation identity.
- [x] 34.2 Main model: reject every in-scope non-regular mode found in either HEAD or the index before computing production or Native source identity.
- [ ] 34.3 Checkpoint: rerun Git/source identity, delivery trust, fake/real Native, strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 35. Closed-report exact JSON type follow-up

- [x] 35.1 Main model: preserve the fresh trust-QA boolean-coercion reproduction and add deterministic red tests for `report_version`, zero/one summary counters, and the unit pass-rate field.
- [x] 35.2 Main model: require exact integer types for report version and every summary counter, and reject booleans from numeric summary fields before `should_fail` evaluates thresholds.
- [ ] 35.3 Checkpoint: rerun report forgery, fixture/fake/real Native, persistent documentation, strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 36. Passing-live exact integer contract follow-up

- [x] 36.1 Main model: preserve the fresh trust-QA bool/float coercion reproductions and add single, parallel, nested-returncode, producer, count, and workload-unit red matrices.
- [x] 36.2 Main model: make the passing-live detail and producer contracts recursively type-exact, and independently require exact producer and top-level token counts.
- [ ] 36.3 Checkpoint: rerun all report forgery, fixture/fake/real Native, persistent documentation, strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 37. Structured-result candidate-stability follow-up

- [x] 37.1 Main model: preserve the final strict-regression failure proving an untracked root result artifact changes the hardened candidate and invalidates the completed execution.
- [x] 37.2 Main model: move the CLI integration fixture's generated structured result into Kafa-owned `.ai-team/runtime/` state without excluding arbitrary project source paths from candidate identity.
- [ ] 37.3 Checkpoint: rerun structured-result, candidate-identity, complete strict regression, real Native profiles, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 38. Runtime-smoke clean-candidate follow-up

- [x] 38.1 Main model: preserve the direct runtime-smoke failure where init-generated `.gitignore` leaves the quality gate dirty, and add an executable regression for the full local delivery scenario.
- [x] 38.2 Main model: commit the init-generated candidate ignore policy inside the smoke fixture before verification without weakening the production dirty-worktree gate.
- [ ] 38.3 Checkpoint: rerun runtime smoke, candidate/gate tests, complete strict regression, real Native profiles, artifacts, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

## 39. Atomic and exact projection publication follow-up

- [x] 39.1 Main model: preserve fresh migration-QA reproductions and add deterministic red tests proving public projection rebuild cannot publish a stale pre-migration view and a silent no-op/corrupt renderer cannot satisfy activation validation.
- [x] 39.2 Main model: hold the project operation lock across every production projection read/write lifecycle and validate all live projection bytes against an independently rendered schema-30 snapshot before migration success.
- [ ] 39.3 Checkpoint: rerun projection rebuild, same-schema migrate, repair, migration activation/rollback, doctor, complete strict regression, real Native profiles, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 40. Git replace-object fail-closed follow-up

- [x] 40.1 Main model: preserve fresh trust-QA replace-ref reproductions and add deterministic red tests proving commit/tree/blob replacements cannot hide a HEAD-only gitlink or substitute a missing production/Native source object.
- [x] 40.2 Main model: disable Git replace-object lookup in every isolated production and Native identity command while retaining ambient-environment, local-object, mode, symlink, and no-fetch protections.
- [ ] 40.3 Checkpoint: rerun replace-ref, missing-object, gitlink, source-identity, delivery trust, fake/real Native, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 41. Exact active-table contract follow-up

- [x] 41.1 Main model: preserve the fresh trust-QA retired-table false pass and add a fake-Native red test that creates an unexpected schema table through allowed candidate execution.
- [x] 41.2 Main model: make passing single/parallel report generation require the exact schema-30 active table inventory so any missing or extra Connector/Host/runtime table fails closed.
- [ ] 41.3 Checkpoint: rerun fake/live Native, retired-surface, schema inventory, report forgery, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 42. Non-negative evaluator-counter follow-up

- [x] 42.1 Main model: preserve the fresh trust-QA counter-cancellation reproduction and add deterministic red tests for negative false-pass, human-intervention, SQLite-lock, and other detail counters.
- [x] 42.2 Main model: require every fixture/stability detail counter to be an exact non-negative integer before aggregation so positive and negative facts cannot cancel.
- [ ] 42.3 Checkpoint: rerun fixture/stability/report-forgery suites, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 43. Sentinel terminal-state and missing-DB guidance follow-up

- [x] 43.1 Main model: add red tests proving sentinel removal after a handled pre-activation failure requires verified unchanged/restored DB and projections, unverified projection-backup failure retains the diagnostic sentinel, and missing-DB recovery state is never reported as uninitialized by status/doctor/validate/quickstart status.
- [x] 43.2 Main model: retain the sentinel until success or explicit complete authority verification, surface manifest/do-not-remove guidance before the missing-DB shortcut, and align spec/public documentation without recommending init during recovery.
- [ ] 43.3 Checkpoint: rerun sentinel, failure injection, missing-DB CLI, documentation, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 44. WAL/SHM rollback-authority follow-up

- [x] 44.1 Main model: preserve the fresh migration-QA live-WAL reproduction and add a cross-platform red test forbidding sentinel-free rollback when failed schema-30 sidecars can replay into the restored source database.
- [x] 44.2 Main model: quarantine and verify failed-schema WAL/SHM before authority restore, validate the restored backup through ordinary SQLite semantics, and retain rollback-incomplete recovery state whenever sidecars or handles cannot be neutralized.
- [ ] 44.3 Checkpoint: rerun WAL/SHM, live-handle, rollback, Windows handle, migration/recovery, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 45. Deterministic project-state projection follow-up

- [x] 45.1 Main model: preserve the broad targeted-suite reproduction and add deterministic red tests proving the same unchanged schema-30 database renders byte-identical `project-state.yaml` under different controlled wall-clock values and authoritative rebuild removes stale ad-hoc keys.
- [x] 45.2 Main model: derive every exact `project-state.yaml` field, including `updated_at`, from SQLite authority and replace rather than merge the generated view while preserving the generic state-writer contract for non-projection callers.
- [ ] 45.3 Checkpoint: rerun deterministic projection, public migration, doctor, delivery validation, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 46. Core-owned projection verification follow-up

- [x] 46.1 Main model: preserve the fresh migration-QA direct-core no-op callback reproduction and add a red test proving a callback that returns without publishing schema-30 views cannot report activation or clear the recovery sentinel without complete rollback.
- [x] 46.2 Main model: make the core migration entrypoint independently verify all live projection bytes after the caller callback and before activation success so callback self-report is never the trust boundary.
- [ ] 46.3 Checkpoint: rerun direct-core/public migration, projection activation/rollback, sentinel, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 47. Repository worktree-config isolation follow-up

- [x] 47.1 Main model: preserve the fresh trust-QA repo-local `core.worktree` reproductions and add red tests proving production and Native source identity remain bound to the explicit evaluated root rather than downgrading or enumerating a redirected worktree.
- [x] 47.2 Main model: pin the trusted worktree root in every isolated production and Native Git environment while retaining local-object, no-replace, no-fetch, mode, symlink, gitlink, and unmerged-entry protections.
- [ ] 47.3 Checkpoint: rerun Git-environment, source-identity, delivery trust, fake/real Native, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 48. Reserved SQLite table inventory follow-up

- [x] 48.1 Main model: preserve the fresh trust-QA writable-schema reproduction and add a red test proving an unexpected queryable `sqlite_*` table cannot be filtered out of the exact active-table contract.
- [x] 48.2 Main model: permit only the explicitly required SQLite internal table inventory in addition to the 27 schema-30 business tables, and fail closed on every other catalog table including Connector/Host names hidden under `sqlite_*`.
- [ ] 48.3 Checkpoint: rerun schema inventory, fake/live Native, runtime doctor, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 49. BaseException-safe operation-lock cleanup follow-up

- [x] 49.1 Main model: preserve the fresh migration-QA cancellation reproductions and add cross-process red tests proving cancellation during lock open or unlock cannot leak a descriptor, OS lock, or process-local lock.
- [x] 49.2 Main model: make every operation-lock acquisition and release cleanup path BaseException-safe while preserving the original cancellation and normal five-second fail-closed behavior.
- [ ] 49.3 Checkpoint: rerun lock normal/exception/cancellation/process-exit/Windows tests, migration concurrency, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 50. Native controller-source stability follow-up

- [x] 50.1 Main model: preserve the fresh trust-QA Event/barrier TOCTOU reproduction and add a deterministic red test proving a transiently modified-and-restored controller source cannot become the bytes executed by a passing Native evaluation.
- [x] 50.2 Main model: capture live evaluation identity at profile start, verify a private Git-backed snapshot against it, run every controller harness subprocess from that immutable snapshot, bind the report to the start identity, and fail if the original source differs at completion.
- [ ] 50.3 Checkpoint: rerun transient-source, source-identity, fake/real Native, persistent-report, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 51. Exact project-state field contract follow-up

- [x] 51.1 Main model: preserve the fresh migration re-QA reproduction and add a red test proving generated `project-state.yaml` has exactly the schema-declared keys, includes DB `id` and `current_cycle_id`, and contains no fabricated `blocked_reason`.
- [x] 51.2 Main model: publish every project-state field from the authoritative project row while suppressing generic writer-only defaults for this exact generated projection.
- [ ] 51.3 Checkpoint: rerun project-state schema/content/rebuild, doctor, migration activation/rollback, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 52. Projection-callback database immutability follow-up

- [x] 52.1 Main model: preserve the fresh migration re-QA callback-write reproduction and add a red test proving a callback cannot inject even doctor-valid schema-30 facts, render matching views, report activation, or clear the sentinel without rollback.
- [x] 52.2 Main model: bind active database authority immediately before and after the publication callback, reject every fingerprint change, and rerun final schema doctor before independent projection verification.
- [ ] 52.3 Checkpoint: rerun direct-core callback mutation/no-op/publication, DB/projection rollback, sentinel, complete strict regression, artifacts, both OpenSpec validations, fresh migration QA, and both complete three-platform matrices before merge.

## 53. Snapshot Git-init environment isolation follow-up

- [x] 53.1 Main model: preserve the fresh trust re-QA ambient `GIT_DIR` reproduction and add a red test proving private snapshot initialization neither creates nor uses an external ambient Git directory.
- [x] 53.2 Main model: run snapshot initialization under the same pinned isolated Git environment as hash/index operations and disable ambient template injection.
- [ ] 53.3 Checkpoint: rerun ambient Git overrides, snapshot/filter/transient-source, fake/real Native, complete strict regression, artifacts, both OpenSpec validations, fresh trust QA, and both complete three-platform matrices before merge.

## 54. Source artifact completeness follow-up

- [x] 54.1 Main model: preserve the real artifact-mode install failure and add a red test proving the source distribution contains one release root with `release.json`, `VERSION`, the complete plugin bundle, and no generated Python cache files.
- [x] 54.2 Main model: define an explicit source-distribution manifest that packages the local-only plugin release root required by isolated install without adding runtime dependencies or retired surfaces.
- [ ] 54.3 Checkpoint: rebuild real wheel/source artifacts, run artifact-mode isolated install, install/release targeted tests, complete strict regression, both OpenSpec validations, fresh QA, and both complete three-platform matrices before merge.

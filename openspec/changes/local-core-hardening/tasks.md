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

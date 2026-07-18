## 1. Contract And Baseline

- [x] 1.1 Main/deep: confirm clean `main==origin/main`, archive the two completed local-core changes, and establish the validated schema-30 canonical spec before this change.
- [x] 1.2 Main/deep: write proposal, design, and delta spec with the static-malicious-repository threat model, same-user attacker boundary, stable errors, rollback rules, and unchanged public inventory.
- [x] 1.3 Validate this change with `openspec status --change canonical-write-path-safety` and `openspec validate canonical-write-path-safety`; capture current parser/schema/plugin inventory and performance baselines.

## 2. Deterministic Red Tests

- [x] 2.1 Add lexical, unsafe-ancestor, final symlink, hard-link, non-regular, root-alias, and identity-change red tests in a dedicated project-filesystem safety suite; use Events/Barriers for races and no correctness sleeps.
- [x] 2.2 Add real POSIX symlink/hardlink/FIFO/socket tests and Windows junction/hardlink/reparse tests; unsupported platform primitives must produce explicit per-case capability results, never skip the whole safety group.
- [x] 2.3 Add Store red tests covering DB, WAL/SHM/journal, operation lock, migration sentinel, normal connection/transaction/backup, BaseException cleanup, and unchanged `InMemoryStore` behavior.
- [x] 2.4 Add projection/init red tests covering all 13 generated paths, retired evidence, `.gitignore`, and three agent-template destinations; prove external sentinel bytes/hash/mode/inode remain unchanged.
- [x] 2.5 Add execution red tests for stdout, structured-result, and container artifact source/destination links and path replacement races; prove no passing execution or validation is recorded.
- [x] 2.6 Add migration/recovery red tests for unsafe backup, staging, manifest, projection-backup, failed-DB, sidecar, restore, and sentinel paths before and after activation.
- [x] 2.7 Run the new targeted suites against unchanged production, confirm each defect class fails for the expected reason, and record the exact red evidence without describing unsupported cases as passes.

## 3. ProjectFS Foundation

- [x] 3.1 Implement the closed relative-path grammar, stable `unsafe-project-path` reasons, one-time root-alias resolution, pinned root identity, and deterministic error rendering in internal `core/project_fs.py`.
- [x] 3.2 Implement POSIX descriptor-relative ancestor walking, safe directory creation, regular/single-link checks, atomic write/replace/unlink, fsync, and identity rechecks.
- [x] 3.3 Implement the Windows handle backend with `CreateFileW`, reparse/file-ID/volume/link-count checks, held ancestor handles without delete sharing, create-new, and fail-closed capability errors.
- [x] 3.4 Implement safe read, exclusive create, lock descriptor, unique directory, copy, and bounded audit operations; ensure every handle/descriptor closes on `BaseException`.
- [x] 3.5 Make the foundation tests green on the current platform, run deterministic backend fakes for the other platform, and run `py_compile` plus `git diff --check`.

## 4. Store, Operation Lock, And SQLite

- [x] 4.1 Key reentrant operation locks by pinned root filesystem identity plus the fixed relative lock path, and route lock/sentinel open/read through one `ProjectFS` held for the entire operation.
- [x] 4.2 Safely precreate or validate the main DB and DB-family paths, connect only with SQLite URI `mode=ro|rw`, and recheck identity after connect, after journal setup, and before close.
- [x] 4.3 Route file-backed connection, transaction, and backup destination publication through the safe seam without changing timeout, WAL, foreign-key, nesting, or `InMemoryStore` semantics.
- [x] 4.4 Run Store/path targeted tests with `ResourceWarning` as error, including process/thread reentrancy, fork cleanup, active migration, and all DB-family attacks.

## 5. Projections And Initialization

- [x] 5.1 Make projection serialization publish through safe atomic writes and make retired projection removal use safe regular-file unlink; keep LF UTF-8 bytes and all 13 renderer outputs unchanged.
- [x] 5.2 Make projection verification use safe reads for live views and preserve independent snapshot rendering without trusting linked expected or actual files.
- [x] 5.3 Preflight the full projection set before any mutation commit, including normal mutation, rebuild, same-schema migration, and repair publication.
- [x] 5.4 Preflight DB family, lock/sentinel, `.gitignore`, projections, retired view, and three template destinations before init's first write; route destination writes/copies through `ProjectFS` while packaged template inputs remain read-only.
- [x] 5.5 Run projection/init targeted tests and byte-compare normal generated views/templates against the pre-change golden outputs.

## 6. Execution And Artifact Evidence

- [x] 6.1 Route local stdout artifact creation/readback and structured-result read/copy through safe project-relative operations while preserving digest, truncation, count, and stale-candidate semantics.
- [x] 6.2 Route container stdout and structured-result destination handling through the same seam and fail before recording passing evidence when any identity is unsafe.
- [x] 6.3 Run execution, structured-result, sandbox-policy, and stop-ship targeted suites; confirm unsafe cases create no passing immutable execution or validation.

## 7. Migration, Backup, And Rollback

- [x] 7.1 Create/update the migration sentinel and manifest with safe exclusive/atomic operations and acquire the safe operation lock before reading source authority.
- [x] 7.2 Route backup, staging, projection-backup directory, failed DB, sidecar quarantine, restore, and cleanup operations through pinned safe paths with verified mode/digest/existence metadata unchanged.
- [x] 7.3 Audit every activation and rollback target before replacement; on any post-activation unsafe path, retain sentinel/manifest and record `rollback-incomplete` with original and restore errors.
- [x] 7.4 Preserve schema 27/28/29 conversion, active DB fingerprinting, mandatory projection validator, hard-exit recovery, and exact projection rollback semantics.
- [x] 7.5 Run migration/rollback/hard-exit targeted suites plus new path attacks; inspect manifests and prove original external objects and source DB remain unchanged.

## 8. Doctor And CLI Integration

- [x] 8.1 Add a bounded canonical-path audit used by runtime status/doctor before SQLite, with migration sentinel guidance taking precedence over uninitialized advice.
- [x] 8.2 Replace `kafa project doctor`'s independent lock/SQLite opener with delegation to the hardened runtime audit while preserving its JSON keys, exit codes, and actionable messages.
- [x] 8.3 Run project-doctor, runtime doctor, uninitialized/recovery, CLI help, and install-release targeted suites on normal and adversarial projects.

## 9. Contract, Performance, And Documentation

- [x] 9.1 Freeze schema 30/27 tables, 53 parser nodes, seven Skills, three Hooks, three templates, local-only runtime, and Native Host ownership in structure/architecture tests.
- [x] 9.2 Document stable path errors, safe remediation, root-alias behavior, arbitrary-command non-sandbox boundary, and `rollback-incomplete` recovery without suggesting automatic link repair.
- [x] 9.3 Run the 5k-fact benchmark and enforce mutation ≤0.050s, DB ≤320 KiB, plugin ≤1 MiB, and existing init/startup budgets.
- [x] 9.4 Checkpoint: run all path/store/projection/init/execution/migration/doctor targeted suites with warnings as errors, structure validation, OpenSpec validation, JSON validation, and `git diff --check`.

## 10. Independent QA And Full Delivery Evidence

- [x] 10.1 Run complete unittest discovery with `ResourceWarning` as error; report exact count and keep skip/expected-failure/not-run distinct.
- [x] 10.2 Run runtime smoke, fixture/stability E2E, Skill eval, isolated wheel/sdist installation, cache/discovery/doctor/hook/uninstall, and artifact checksum validation.
- [x] 10.3 Regenerate real Native single and parallel reports for the exact source candidate; validate source/status/binary/token/scope/timing consistency and do not infer token savings.
- [x] 10.4 Independent read-only QA A reviews POSIX/Windows filesystem and SQLite/store safety; QA B reviews migration/recovery and execution-evidence safety. Main/deep applies every fix, then reruns and re-reviews.
- [ ] 10.5 Push only after local gates pass, require both push and pull-request Ubuntu/macOS/Windows matrices for the exact implementation head, and record annotations separately from failures.
- [x] 10.6 Complete adversarial review for logic gaps, incorrect facts, simpler alternatives, same-user threat overclaims, data loss, stale candidate, and missing evidence; resolve every Critical/High/Medium finding.

## 11. Archive And Merge

- [ ] 11.1 Update the final audit with red/green evidence, before/after metrics, exact local and CI run IDs, QA findings, residual risks, and explicit no-tag/no-release/no-deploy status.
- [ ] 11.2 Mark every checklist item from actual evidence, validate the completed change strictly, and archive it into the canonical `local-delivery-kernel` spec.
- [ ] 11.3 Run the post-archive documentation contract and exact final-source checks, commit/push the closure, wait for all six matrix jobs, and normally merge the PR.
- [ ] 11.4 Fetch/prune, confirm local `main==origin/main` and the PR is merged, remove only the merged feature branch, and verify the current user plugin installation was not replaced.

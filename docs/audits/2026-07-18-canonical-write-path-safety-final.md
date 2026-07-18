# Canonical Write Path Safety Final Audit

## Audit identity and disposition

- Change: `canonical-write-path-safety`.
- Branch: `v2-canonical-write-path-safety`.
- Baseline `main`: `a24f69a1cae0f9628e4e2632c5948cbf3f366339`.
- Exact production candidate:
  `22523b53d9d26c790dae49f763eed2bc5a8c4bc5`.
- Runtime / kernel / schema: `5.0.0` / `5.0.0` / `30`.
- Source candidate: `2.0.0-beta.1` (`2.0.0b1`, development state).
- Public inventory remains 27 active tables, 53 recursive CLI parser nodes,
  seven Skills, three Hooks, three agent templates, 16 JSON schemas, and 13
  generated projections.

This audit supersedes the earlier robustness conclusion for canonical project
paths. It does not replace the local-core slimming or hardening history. The
candidate adds one internal `ProjectFS` authority and routes canonical writes,
reads, locks, SQLite authority, execution evidence, migration, rollback,
initialization, projections, and doctor preflight through it. It does not add a
business table, schema version, lifecycle owner, Connector, provider worker, or
network dependency.

Publication, archive, and merge evidence is filled only from completed jobs.
Until the exact-head matrices finish, remote Windows/Linux/macOS status remains
`not-run`; local deterministic Windows fakes are not substituted for native
Windows evidence.

## Delivered behavior

- Relative paths use a closed grammar. The project root alias is resolved once,
  root identity is pinned, and descendant links, non-regular files, multi-link
  files, reparse points, unsafe ancestors, and identity changes fail closed with
  stable `unsafe-project-path` diagnostics.
- POSIX operations walk descriptor-relative ancestors and use held descriptors,
  create-new/replace/unlink, fsync, and post-syscall identity reconciliation.
  The Windows backend uses held handles, file/volume identity, link-count and
  reparse checks, and fails closed when a required capability is unavailable.
- File-backed Store operations key the reentrant operation lock by pinned root
  identity and use verified SQLite `mode=ro|rw` paths. The main DB and
  WAL/SHM/journal family are checked before connect, after journal setup, and
  before close. `InMemoryStore` keeps its test-only public semantics and now
  also rolls back `BaseException` cancellation without poisoning its next
  transaction.
- All 13 projections, retired evidence removal, `.gitignore`, and the three
  Native agent-template destinations are preflighted before publication.
- Local and container stdout plus structured-result artifacts are published and
  read through safe project-relative operations. Passing execution or
  validation facts are not persisted after an unsafe identity or stale
  candidate result.
- Migration safely creates its sentinel and manifest, acquires the same
  operation lock before reading authority, and verifies backup, staging,
  activation, projection backup, failed DB, sidecars, restore, and recovery
  paths. DB-family, projection, recovery-bundle, manifest, and sentinel receipts
  are reverified through terminal publication.
- Post-activation failure restores verified DB and projection authority. If
  rollback cannot be proved, the sentinel remains and a verified manifest
  records `rollback-incomplete`, original error, restore error, and diagnostic
  paths; unknown authority is never relabeled successful.
- Runtime status/doctor audits the bounded canonical path set before SQLite.
  `kafa project doctor` delegates to that authority and preserves
  `migration-in-progress` guidance and existing JSON/exit contracts.

Native Codex/ChatGPT remains the only owner of task/thread, subagent, worktree,
approval, model, cancellation, steering, and handoff lifecycle. Kafa remains a
local-only verified delivery kernel.

## Red-to-green closure

The pre-production red suite ran against `0facd65` plus tests only. It reported
`run=36 failures=47 errors=5 skipped=1`, exit 1. The five errors represented the
intentionally absent `core.project_fs` contract; the one skipped case was the
macOS-inapplicable native Windows junction case and was not called passing. The
unchanged positive migration/execution contract passed 37/37.

| Defect class | Red behavior | Closed behavior |
| --- | --- | --- |
| Lexical/ancestor authority | Absolute, traversal, linked ancestor, root alias, non-regular and multi-link authority could reach pathname operations | Closed grammar, pinned root and held-ancestor identity reject the operation before trusted publication. |
| Final-target race | A target could be exchanged after validation or at replace/unlink time | Final syscall reconciliation pins expected source/destination identity and rolls back or fails closed on mismatch. |
| SQLite family | DB, WAL/SHM/journal, lock, sentinel and backup destinations could follow unsafe authority | Store and migration hold one `ProjectFS` and operation lock through the entire SQLite/backup lifecycle. |
| Init/projection partial mutation | Unsafe `.gitignore`, projections, retired evidence or templates could be discovered after mutation began | The entire bounded set is preflighted; publication uses safe atomic writes and safe unlink. |
| Execution false pass | Linked stdout or structured results and same-content exchanges could record passing facts | Local/container artifacts are identity-pinned; unsafe or stale evidence produces no passing immutable execution/validation. |
| Parser false pass | Structured test formats could accept ambiguous, truncated or forged result authority | JUnit, pytest, Jest, Playwright and nextest parsing now uses closed, exact result contracts and fail-closed source handling. |
| Migration/recovery split | Unsafe backup/staging/manifest/restore paths could activate or appear to complete rollback | Every activation/rollback target and terminal receipt is verified; incomplete recovery retains sentinel and manifest authority. |
| Doctor bypass | Wrapper doctor could independently open lock/SQLite paths | Wrapper and runtime share the bounded no-open preflight before SQLite. |
| Windows mode metadata | Migration captured a safe snapshot and then used pathname-following `stat()` for mode bits | Windows mode is derived only from the handle-backed snapshot's readonly attribute; no pathname metadata read remains. |
| In-memory cancellation | `KeyboardInterrupt` left the test Store transaction open and broke the next transaction | `BaseException` rolls back, preserves a rollback failure as a note, and the next transaction succeeds. |

No adversarial test was removed or weakened to obtain green results. Event and
Barrier coordination, rather than correctness sleeps, drives replacement and
writer/migration races.

## Local validation

Every passing row below was executed for source candidate `22523b5`. Evidence
profiles remain distinct: fixture checks do not prove Native Host behavior, and
deterministic Windows fakes do not prove native Windows behavior.

| Gate | Result |
| --- | --- |
| Complete strict unittest discovery | 549 total in 198.848 s; 539 actual passed, 10 native-Windows cases skipped/not-run, 0 failure, 0 error, 0 expected failure; `ResourceWarning` promoted to error |
| ProjectFS/Store/migration/execution/structured combined | 235 total; 225 actual passed, the same 10 native-Windows cases skipped/not-run, 0 failure/error; rerun outside the restricted socket sandbox |
| QA-B migration/execution/structured targeted | 94/94 passed; separate 8/8 recovery/artifact attacks and 8/8 parser false-pass cases also passed |
| Runtime smoke | 2/2; all 15 lifecycle return codes zero; directed/full invariant ratio 48.022462 vs 10x minimum |
| Skill transcript fixture | 17/17 required ordered markers; fixture evidence only |
| Fixture E2E | 6/6 in 6.780291 s; skipped, failure, false-pass, SQLite-lock and human-intervention counts all zero |
| Stability E2E | 11/11 in 10.842731 s; skipped, failure, false-pass, SQLite-lock and human-intervention counts all zero |
| OpenSpec before publication | 4/4 artifacts; strict validation passed |
| Native persistent report consistency | Single and parallel reports each returned `[]` against current source, Git state, binary and matrix contract |

The immediately preceding full attempt ran 549 tests in 205.606 s and had one
failure because the reports still named the old source SHA. That attempt is
failed evidence, not reused as a pass; both Native profiles were regenerated
before the successful complete rerun above.

The source repository intentionally has no `.ai-team` runtime. It was not
initialized to manufacture delivery facts; OpenSpec, exact-source tests, Native
reports, independent QA, artifact smoke and CI are the delivery authorities for
this source change.

## Real Native Codex evidence

The user explicitly authorized sending only the synthetic task prompts and
temporary test-repository files to `chatgpt.com`. The Host workspaces contained
the bounded `candidate.py` or `alpha.py` / `beta.py` fixtures and their tests;
the complete Kafa repository, business projects and secrets were not Host
workspaces. The controller used its private Kafa snapshot locally.

Both successful reports bind:

- Git HEAD: `22523b53d9d26c790dae49f763eed2bc5a8c4bc5`;
- executable workspace SHA-256:
  `9bccc0c8dde662d98e146aa23453a7c61967263831bad14ae496cec9dbd25259`;
- clean status SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`;
- status entries: 0;
- Codex CLI: `0.143.0`;
- Native binary SHA-256:
  `d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.

| Profile | Workload/result | Tokens | Native timing | Controller verification |
| --- | --- | ---: | ---: | --- |
| Single | one unit; only `candidate.py`; passed | 51,306 | 26.355625 s | one structured target, rc=0 |
| Parallel | two disjoint units; `alpha.py` / `beta.py`; passed | 104,224 | 29.031020 s wall; 28.774869 s overlap | two targeted plus combined verification, all rc=0 |

Parallel used 52,065 and 52,159 tokens for its two producer units. It proves
real wall-clock overlap for independent work, not token savings: total parallel
tokens are about twice the single profile. Shared-context/single execution stays
the conservative default; parallel fan-out is reserved for independent bounded
work with deterministic integration checks. Host model identity, monetary cost
and delivery provenance are not exposed and are not inferred.

Before authorization, two sandboxed single attempts failed solely on DNS for
the WebSocket and HTTPS Host endpoints, with no token record, no last message
and no controller run. They remain recorded as failed attempts and are not
confused with the later successful evidence.

## Performance, size, and code-shape truth

The final five-sample report is
`docs/audits/2026-07-16-canonical-write-path-safety-benchmark.json`. Timing is
comparative evidence, not a portable CI assertion. The explicit hard gates are
mutation at most 0.050 s, DB at most 320 KiB, and Plugin payload at most 1 MiB.

| Metric | Change baseline | Candidate | Result |
| --- | ---: | ---: | --- |
| Fresh DB | 315,392 B | 315,392 B | within 320 KiB |
| Plugin payload file bytes, caches excluded | 695,552 B | 1,021,747 B | within 1 MiB; 26,829 B headroom |
| Fresh init median | 0.092643 s | 0.161116 s | measured; no new numeric gate |
| One mutation after 5k facts | 0.004734 s | 0.017312 s | pass vs 0.050 s |
| Targeted three-view projection | 0.003168 s | 0.013871 s | measured |
| Full 13-view projection | 0.024034 s | 0.065813 s | measured |
| Strict full suite | 375 / 146.518 s | 549 / 198.848 s | suite green; below existing 300 s reference |
| In-scope Python LOC | 32,794 | 49,360 | +50.52% |
| Test Python LOC | 14,750 | 22,421 | +52.01% |
| Plugin Python LOC | 15,903 | 24,866 | +56.36% |

The safety seam and adversarial coverage materially expand code and tests. The
previously user-accepted slimming LOC deviation remains historical truth; it is
not relabeled as a metric pass, and this audit does not claim that the new LOC
growth satisfies the old slimming target. The current change instead satisfies
its locked runtime payload and performance budgets while preserving the exact
public inventory.

## Artifact and isolated installation evidence

The real PEP 517 build used the workflow-pinned `build==1.5.1` and produced:

- wheel `kafa-2.0.0b1-py3-none-any.whl`, SHA-256
  `db5109ed04d198c5a2cd42794888d08ed41bf2ef786a0aca7feb6df3d854f886`;
- sdist `kafa-2.0.0b1.tar.gz`, SHA-256
  `621225e74dd4a85c898e23afaa0d64537aac511ecb57494f24e0d2f941c47806`.

Artifact mode passed in a temporary venv, HOME and CODEX_HOME. It verified wheel
import isolation, sdist manifest identity, marketplace and cache discovery,
Codex app-server discovery, exact seven Skills / three Hooks / three templates /
16 schemas / seven runtime scripts / nine runtime anchors, schema-30 init,
quickstart, doctor, retired-surface absence, cached and direct Hook handlers, and
Codex plugin removal. The smoke did not claim a live authenticated Host Hook
turn (`host_hook_execution_observed=false`). `kafa plugin uninstall
--remove-files` is covered by the strict unit suite, not misreported as part of
the artifact smoke.

The active user installation was not replaced by this isolated run. Current
machine truth is `kafa 2.0.0-beta.1` and
`codex-project-harness@personal 2.0.0-beta.1` installed/enabled; the earlier
`1.25.0-beta.1` handoff note is stale. The pre-merge installed-plugin tree
digest, excluding bytecode caches, is
`813ab6d183e180149eef4adc57af8412d5994e8fcac728edc120515a837c0a90` and
must remain equal after merge.

## Independent QA and adversarial review

Three independent read-only reviews now have zero open Critical, High or Medium
finding for exact source candidate `22523b5`:

- QA A reviewed POSIX/Windows ProjectFS, SQLite/Store and operation-lock safety.
  It found the Medium `InMemoryStore` cancellation leak. Main added a
  deterministic red test, fixed BaseException rollback, ran 23/23, and QA A
  re-reviewed the committed content and signed off.
- QA B reviewed migration/recovery plus execution-evidence safety. It ran 94/94
  targeted cases, 8/8 recovery/artifact attacks, and an 8/8 fail-closed parser
  matrix. It signed exact HEAD `22523b5`, source hash
  `32510902c64283f6e7334dc97245124d3ab46f10440f997008b51c98229e9c25`,
  with Critical/High/Medium/Low = 0/0/0/0.
- The adversarial reviewer found the Medium Windows pathname-`stat()` gap. Main
  demonstrated the failure, derived mode from the pinned handle-backed
  attributes, added readonly/writable red-green coverage, and received a final
  re-review with Critical/High/Medium = 0.

The adversarial scope explicitly included logical gaps, factual errors, simpler
alternatives, same-user threat overclaims, data loss, stale candidates, missing
evidence, incomplete rollback, Windows evidence, and public-inventory drift.
The restricted sandbox's Unix-socket `EPERM` is recorded as a test-environment
event; the identical 235-test selection passed outside that restriction. Native
Windows remains a CI requirement, not a local pass.

## Remote CI and publication truth

The exact implementation/evidence publication and closure publication each
require a push-event and pull-request-event matrix, with Ubuntu, macOS and
Windows separately successful. Run IDs and annotations are recorded only after
GitHub completes them; warnings are not treated as failed jobs, and failed,
cancelled or not-run jobs are never called passing.

| Revision | Push Ubuntu/macOS/Windows | PR Ubuntu/macOS/Windows |
| --- | --- | --- |
| Implementation/evidence | not-run | not-run |
| Closure | not-run | not-run |

## Residual boundaries

- The contract protects against a static malicious repository and bounded
  same-user replacement races. It does not claim to defeat a same-OS-user
  attacker that can continuously mutate held authority or a privileged attacker;
  those require OS-user/container isolation.
- Arbitrary commands passed to `verify run` are not sandboxed by this change.
- The root symlink is resolved once; descendant links fail closed. Kafa does not
  automatically repair, copy, unlink or rewrite external targets.
- SQLite uses verified pathnames and identity checkpoints rather than a custom
  VFS. The documented same-user continuous-replacement boundary remains.
- When rollback authority cannot be verified, availability is deliberately
  sacrificed: sentinel and `rollback-incomplete` diagnostics remain.
- Native reports prove local capability and report consistency, not independent
  delivery provenance.
- Native Windows remains `not-run` locally until GitHub's Windows jobs execute.
- The Plugin payload is within its cap with limited headroom; future growth must
  be measured rather than assumed safe.

No tag, release, deploy, production/business-project migration, secret change,
or current user-plugin replacement is authorized or performed by this change.

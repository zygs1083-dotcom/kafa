# Canonical Write Path Safety Final Audit

## Audit identity and disposition

- Change: `canonical-write-path-safety`.
- Branch: `v2-canonical-write-path-safety`.
- Baseline `main`: `a24f69a1cae0f9628e4e2632c5948cbf3f366339`.
- Exact production candidate:
  `c99bf9bc0648079aa6823c0356599db9d58c84a1`.
- Exact source/test/evidence candidate and exact implementation CI head:
  `889fee31c90a294af0aa5330448989e51a0c0ab6`.
  The final commit changes only tests and evidence. Production and wheel
  payload inputs are unchanged from `c99bf9b`; the final sdist includes the
  test delta and was built from the exact `889fee31` workspace.
- Runtime / kernel / schema: `5.0.0` / `5.0.0` / `30`.
- Source candidate: `2.0.0-beta.1` (`2.0.0b1`, development state).
- Public inventory remains 27 active tables, 53 recursive CLI parser nodes,
  seven Skills, three Hooks, three agent templates, 16 JSON schemas, and 13
  generated projections.

This audit supersedes the earlier robustness conclusion for canonical project
paths. It does not replace the local-core slimming or hardening history. The
change adds one internal `ProjectFS` authority and routes canonical writes,
reads, locks, SQLite authority, execution evidence, migration, rollback,
initialization, projections, and doctor preflight through it. It adds no
business table, schema version, lifecycle owner, Connector, provider worker, or
network dependency.

The exact `889fee31` push run `29649886701` and pull-request run `29649888195`
each completed successfully on Ubuntu, macOS, and native Windows. Earlier
Windows failures remain failed historical evidence below; no corrected run is
used to relabel them. OpenSpec archive completed locally as
`2026-07-18-canonical-write-path-safety`; closure publication is still
`not-run` at this audit checkpoint.

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
  before close. `InMemoryStore` keeps its test-only public semantics and rolls
  back `BaseException` cancellation without poisoning its next transaction.
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
| Parser false pass | Structured test formats could accept ambiguous, truncated or forged result authority | JUnit, pytest, Jest, Playwright and nextest parsing use closed, exact result contracts and fail-closed source handling. |
| Migration/recovery split | Unsafe backup/staging/manifest/restore paths could activate or appear to complete rollback | Every activation/rollback target and terminal receipt is verified; incomplete recovery retains sentinel and manifest authority. |
| Doctor bypass | Wrapper doctor could independently open lock/SQLite paths | Wrapper and runtime share the bounded no-open preflight before SQLite. |
| Windows mode metadata | Migration captured a safe snapshot and then used pathname-following `stat()` for mode bits | Windows mode is derived only from the handle-backed snapshot's readonly attribute; no pathname metadata read remains. |
| In-memory cancellation | `KeyboardInterrupt` left the test Store transaction open and broke the next transaction | `BaseException` rolls back, preserves a rollback failure as a note, and the next transaction succeeds. |
| Windows readonly replacement source | Atomic-write temporary sources could inherit readonly metadata, causing `ReplaceFileW` to fail with `ERROR_ACCESS_DENIED 5` | The exact source is reopened with `FILE_WRITE_ATTRIBUTES`, identity-checked through its pinned handle, made writable only for replacement, restored on failure, and interruption paths reconcile fail closed. |
| Windows sharing-denial observation | A real CRT write-sharing denial could surface as `EACCES` without `winerror`, while hardlink behavior was incorrectly assumed to match rename/write sharing | CRT `EACCES` is accepted only for the scoped write probe. Rename/write remain blocked; a successful hardlink race is detected by the pinned final handle's link count, rolls the canonical target back, and fails `hard-linked-target`. |

No adversarial test was deleted or weakened to obtain green results. The final
Windows test models actual semantics: an exclusive handle blocks rename and
write, but hardlink creation can succeed; production must detect that new link,
restore canonical authority, and fail closed. Event/Barrier coordination,
rather than correctness sleeps, drives replacement and writer/migration races.

## Local validation

The final local evidence binds the executable workspace bytes later committed
as `889fee31`. Unsupported native-Windows cases on macOS are `not-run`, not
passes. Fixture checks do not prove Native Host behavior, and deterministic
Windows fakes do not replace the two native-Windows CI jobs.

| Gate | Result |
| --- | --- |
| Complete strict unittest discovery | 571 total in 236.292 s; 559 actual passed, 12 native-Windows-only cases skipped/not-run, 0 failure, 0 error, 0 expected failure; `ResourceWarning` promoted to error |
| Final ProjectFS safety suite | 158 total; 146 actual passed and the same 12 native-Windows-only cases skipped/not-run; 0 failure/error |
| Runtime smoke | 2/2; all 15 lifecycle return codes zero; directed/full invariant ratio 42.331626927 vs 10x minimum |
| Skill transcript fixture | 17/17 required ordered markers; fixture evidence only |
| Fixture E2E | 6/6 in 8.164938 s; skipped, failure, false-pass, SQLite-lock and human-intervention counts all zero |
| Stability E2E | 11/11 in 12.877924 s; skipped, failure, false-pass, SQLite-lock and human-intervention counts all zero |
| Documentation contract | 21/21 passed before archive with `ResourceWarning` promoted to error |
| Structure/release/JSON controls | Structure and development-state release contracts passed; changed persistent JSON files validated |
| OpenSpec before archive | 4/4 artifacts; change validation and `--all --strict` validation passed |
| OpenSpec after archive | No active changes; canonical `local-delivery-kernel` has 23 requirements; spec and `--all --strict` validation passed |
| Post-archive documentation contract | 21/21 passed with `ResourceWarning` promoted to error |
| Post-archive documentation/install/architecture contracts | 59/59 passed in 29.969 s outside the restricted network sandbox; the first sandboxed attempt failed 2 build-isolation cases solely because PyPI DNS was blocked and remains failed setup evidence |
| Native persistent report consistency | Single and parallel each returned `[]`; captured Git metadata remains separate from the identical current executable workspace bytes |
| Whitespace and tree state | `git diff --check` passed; exact implementation head and its remote tracking ref were equal and the tree was clean before this audit edit |

The benchmark JSON records `c99bf9b` with a dirty working tree because the
final test/evidence delta had not yet been committed. Its executable workspace
bytes are exactly those at clean `889fee31`; the report is not rewritten to
pretend it captured a different Git state. The source repository intentionally
has no `.ai-team` runtime and was not initialized to manufacture delivery facts.

## Real Native Codex evidence

The user explicitly authorized sending only synthetic task prompts and
temporary test-repository files to `chatgpt.com`. The Host workspaces contained
only the bounded `candidate.py` or `alpha.py` / `beta.py` fixtures and tests;
the Kafa repository, business projects, and secrets were not Host workspaces.
The controller used its private Kafa snapshot locally.

Both successful reports captured:

- Git HEAD `c99bf9bc0648079aa6823c0356599db9d58c84a1`;
- dirty state `true`, with one scoped status entry;
- status SHA-256
  `0f605b6ba0ab19453e1d0abde7fbab0bbb2e0d580e095e1137d55fad311fc891`;
- executable workspace SHA-256
  `822bb4e762a0ae7dd63f73a70471e94eba7c1fa1a7d57583775c5ca8f85edb3b`;
- Codex CLI `0.143.0` and Native binary SHA-256
  `d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.

At clean `889fee31`, the executable workspace SHA remains `822bb4e...`; both
persistent consistency checks return `[]`. The reports therefore bind the
exact executable bytes, while truthfully retaining their pre-commit Git
metadata.

| Profile | Workload/result | Tokens | Native timing | Controller verification |
| --- | --- | ---: | ---: | --- |
| Single | one unit; only `candidate.py`; passed | 51,892 | 53.657465 s | 55.511185 s; one structured target, rc=0 |
| Parallel | two disjoint units; `alpha.py` / `beta.py`; passed | 115,682 | 81.432381 s wall; 77.067911 s overlap | 86.474631 s; two targeted plus combined verification, all rc=0 |

Parallel producer units used 51,169 and 64,513 tokens. Parallel used about
2.23 times the single tokens and was about 52% slower in Native wall time in
this run despite real overlap. The evidence therefore favors single/shared
context by default. Parallel fan-out is reserved for independent bounded work
whose expected latency benefit justifies the additional context and integration
cost. Host model identity, monetary cost, and delivery provenance are not
exposed and are not inferred.

## Performance, size, and code-shape truth

The five-sample report is
`docs/audits/2026-07-16-canonical-write-path-safety-benchmark.json`. Timing is
comparative evidence, not a portable CI assertion. The explicit hard gates are
mutation at most 0.050 s, DB at most 320 KiB, and Plugin payload at most 1 MiB.

| Metric | Change baseline | Candidate | Result |
| --- | ---: | ---: | --- |
| Fresh DB | 315,392 B | 315,392 B | within 320 KiB; 12,288 B headroom |
| Plugin payload, caches excluded | 695,552 B | 1,044,089 B | within 1 MiB; 4,487 B headroom |
| Fresh init median | 0.092643 s | 0.202544 s | measured; no new numeric gate |
| One mutation after 5k facts | 0.004734 s | 0.017152 s | pass vs 0.050 s |
| Targeted three-view projection | 0.003168 s | 0.013245 s | measured |
| Full 13-view projection | 0.024034 s | 0.064199 s | measured |
| Strict full suite | 375 / 146.518 s | 571 / 236.292 s | green; below existing 300 s reference |
| In-scope physical Python LOC | 32,794 | 51,725 | +57.73% |
| Test physical Python LOC | 14,750 | 24,149 | +63.72% |
| Plugin physical Python LOC | 15,903 | 25,503 | +60.37% |

The safety seam and adversarial coverage materially expand code and tests. The
user-accepted slimming LOC deviation remains historical truth; it is not
relabeled as satisfying the earlier slimming metric. The current change instead
satisfies its locked runtime payload and performance budgets while preserving
the exact public inventory. Payload headroom is only 4,487 bytes, so future
growth must be measured.

## Artifact and isolated installation evidence

The real PEP 517 build used the workflow-pinned `build==1.5.1` in a temporary
build environment and produced:

- wheel `kafa-2.0.0b1-py3-none-any.whl`, 30,030 B, SHA-256
  `5e46738eeba03387a6f8e448ea903e17146d627ab2abeedaec38956a71c0d67b`;
- sdist `kafa-2.0.0b1.tar.gz`, 370,134 B, SHA-256
  `dbbc47c4cea7783ccf885c238bf9c4263e3e9adea643fc4a7ee3e43c9de8e8d6`.

The retained artifacts map byte-for-byte to current source: 110 mappable sdist
files and five wheel `kafa/*.py` files have zero mismatch. Metadata is Name
`kafa`, Version `2.0.0b1`, Requires-Python `>=3.11`, with no `Requires-Dist`.

The observed isolated smoke used a temporary venv, HOME, and CODEX_HOME and
returned `ok=true`. It verified discovery and cache identity, exact seven
Skills / three Hooks / three templates / 16 schemas, schema-30 init, status,
quickstart, doctor, direct and cached Hook handlers, and Codex plugin removal.
Its console result was not stored as a durable JSON receipt, so artifact hashes
and contents are independently reproducible while the smoke success remains
bound to the recorded run transcript. It did not claim a live authenticated
Host Hook turn (`host_hook_execution_observed=false`).

The active user installation was not replaced. Pre-merge machine truth is
`kafa 2.0.0-beta.1` and `codex-project-harness@personal 2.0.0-beta.1`
installed/enabled. The installed-plugin digest, excluding bytecode caches, is
`e505042f69cfcf4024d4a93f5b5593edaef192965b7093061152715af63949ed`.
The official user-scope doctor reports the deployment source, installed tree,
and Codex cache at that same digest; it must remain equal after merge.

## Independent QA and adversarial review

Independent read-only reviews now have zero open Critical, High, or Medium
finding for the exact candidate:

- QA A reviewed POSIX/Windows ProjectFS, SQLite/Store, operation locks, and the
  final Windows readonly-source/share-conflict deltas. The earlier Medium
  `InMemoryStore` cancellation leak was fixed with deterministic red/green
  coverage and re-reviewed.
- QA B reviewed migration/recovery, projection coherence, and execution
  evidence. Targeted recovery/artifact and structured-parser attacks remained
  fail closed with no passing fact forged from unsafe authority.
- Adversarial review found and closed the Windows pathname-`stat()` gap, missing
  ancestor handling, x64 `FILE_RENAME_INFO` buffer sizing, CRT `EACCES`
  observation seam, and readonly replacement-source handling.
- The final Windows hardlink review confirmed the corrected platform contract:
  hardlink creation may succeed while rename/write are denied. Production
  rechecks link count via the pinned handle, rolls canonical target bytes back,
  and returns `hard-linked-target`; no production relaxation was needed.

The final two independent delta reviews each reported Critical/High/Medium =
0/0/0. The full review scope included logical gaps, factual errors, simpler
alternatives, same-user threat overclaims, data loss, stale candidates, missing
evidence, incomplete rollback, Windows semantics, and public-inventory drift.

## Remote CI and publication truth

Every published failure remains visible. Counts describe the Windows job; a
later green run does not change the earlier result.

| Revision | Push Ubuntu/macOS/Windows | PR Ubuntu/macOS/Windows |
| --- | --- | --- |
| `3794c7d` first publication | run `29638103502`: success / success / **failed** (20 failures, 9 errors) | run `29638189380`: success / success / **failed** (20 failures, 9 errors) |
| `1cec82d` corrected implementation | run `29639496313`: success / success / **failed** (60/61 targeted passed; one POSIX-only assertion) | run `29639497554`: success / success / **failed** (same assertion) |
| `95aab58` expanded Windows fixtures | run `29640101928`: success / success / **failed** (551 total; 7 failures, 4 errors, 27 skipped) | run `29640103761`: success / success / **failed** (551 total; 7 failures, 4 errors, 27 skipped) |
| `cf02a71` canonical race fixes | run `29643657623`: success / success / **failed** (567 total; 2 errors, 27 skipped) | run `29643658989`: success / success / **failed** (same two errors) |
| `c99bf9b` readonly-source fix | run `29648440031`: success / success / **failed** (570 total; 1 failure, 27 skipped; incorrect test assumption that an exclusive handle blocks hardlink creation) | run `29648441419`: success / success / **failed** (same assertion) |
| Exact `889fee31` source/test/evidence | run `29649886701`: **success / success / success** (Windows: 571 total, 27 skipped) | run `29649888195`: **success / success / success** (Windows: 571 total, 27 skipped) |
| Closure head | not-run | not-run |

All six successful `889fee31` jobs have one warning annotation: GitHub reports
Node.js 20 deprecation for `actions/checkout@v4` and `actions/setup-python@v5`
and currently forces those actions to Node.js 24. The annotation is recorded as
a maintenance warning, not a failed test. Platform-conditional skipped steps
inside a successful job remain skipped; they are not described as passes.

## Residual boundaries

- The contract protects against a static malicious repository and bounded
  same-user replacement races. It does not claim to defeat a same-OS-user
  attacker that can continuously mutate held authority or a privileged
  attacker; those require OS-user/container isolation.
- Arbitrary commands passed to `verify run` are not sandboxed by this change.
- The root symlink is resolved once; descendant links fail closed. Kafa does not
  automatically repair, copy, unlink, or rewrite external targets.
- SQLite uses verified pathnames and identity checkpoints rather than a custom
  VFS. The documented same-user continuous-replacement boundary remains.
- When rollback authority cannot be verified, availability is deliberately
  sacrificed: sentinel and `rollback-incomplete` diagnostics remain.
- In the Windows hardlink race, Kafa restores the canonical project target but
  cannot delete or rewrite an attacker-created alias outside project authority;
  the alias can retain the replacement bytes. A failed replacement's internal
  temporary may remain as diagnostic evidence rather than be unsafely removed.
- Native reports prove local Host capability and report consistency, not
  independent delivery provenance.
- Plugin payload headroom is only 4,487 B. The Node 20 action annotation is a
  real maintenance item even though the exact matrices are successful.
- Closure matrices remain `not-run` until the archive commit is pushed.

No tag, release, deploy, production/business-project migration, secret change,
or current user-plugin replacement is authorized or performed by this change.

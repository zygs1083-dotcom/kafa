# Local Core Slimming Before/After Audit

## Audit identity

- OpenSpec change: `local-core-slimming`
- Baseline: `main@adba3691d859c0ffc93d75cc148d8f916314cc49`
- Baseline remote: `origin/main@adba3691d859c0ffc93d75cc148d8f916314cc49`
- Candidate branch: `v2-local-core-slimming`
- Candidate state: uncommitted working tree, as required
- Release/package: `2.0.0-beta.1` / `2.0.0b1`
- Runtime/Kernel: `5.0.0`
- Database schema: `30`
- Release state: `development`

This audit compares the locked baseline with the current local-only candidate. It
does not claim commit, push, merge, tag, release, or deployment.

## Outcome

The candidate is a local-only verified delivery kernel. Native Codex/ChatGPT
owns tasks, subagents, worktrees, approvals, models, cancellation, and handoff.
Kafa stores local delivery facts and performs controller-side verification; it
does not run a second Host/provider lifecycle and has no business-runtime path
to GitHub, Linear, Notion, Figma, Slack, Connector tokens, `gh api`, or Host SDK
workers.

All functional, migration, trust, local E2E, and regression gates pass. One
locked design target remains unmet: total Python and test LOC fell 28.6% and
32.5%, below the 35%-45% code target. The earlier audit statement that 32.1%
satisfied that target was incorrect. A final exact-surface audit found no dead
`harness_db.py` function: 99 of 101 top-level functions are transitively
reachable and the remaining two are dynamically invoked. Only 4-12 production
LOC are unconditionally removable without changing public behavior. Even the
theoretical set that also breaks optional API/CLI compatibility is only about
143 production and 42 test LOC, versus 1,811 production and 327 test LOC still
needed. Forcing the target would remove supported schema 27/28 migration,
rollback, or negative trust coverage. On 2026-07-12 the user explicitly accepted
this measured LOC deviation and authorized closing task 11.16. The acceptance
does not relabel the unmet target as passed.

The primary strict regression completed with ResourceWarning promoted to error:

```text
Ran 258 tests in 82.718s
OK
real 82.99
user 63.65
sys 15.81
```

No skipped, blocked, not-run, or fixture-only profile is counted in that result.

## Before/after metrics

| Metric | Baseline | Schema 30 candidate | Delta | Reduction |
| --- | ---: | ---: | ---: | ---: |
| In-scope candidate files | 198 | 143 | -55 | 27.8% |
| Python source files | 87 | 63 | -24 | 27.6% |
| Plugin payload files | 118 | 66 | -52 | 44.1% |
| In-scope Python LOC | 33,521 | 23,927 | -9,594 | 28.6% |
| Test Python LOC | 13,251 | 8,940 | -4,311 | 32.5% |
| Plugin Python LOC | 18,878 | 12,971 | -5,907 | 31.3% |
| Runtime tables | 54 | 27 | -27 | 50.0% |
| SQLite indexes, including autoindexes | 67 | 43 | -24 | 35.8% |
| JSON schemas | 40 | 16 | -24 | 60.0% |
| Recursive CLI parser nodes | 129 | 53 | -76 | 58.9% |
| Skill entrypoints | 12 | 7 | -5 | 41.7% |
| Default Hooks | 5 | 3 | -2 | 40.0% |
| Fresh database | 552,960 bytes | 315,392 bytes | -237,568 bytes | 43.0% |
| Plugin directory, caches excluded | 1,276 KiB | 752 KiB | -524 KiB | 41.1% |
| Fresh init median | 0.310000 s | 0.114920 s | -0.195080 s | 62.9% |
| One mutation after 5,000 local facts, median | 0.146113 s | 0.004390 s | -0.141723 s | 97.0% |
| Strict full-suite wall time | 406.72 s / 370 tests | 82.99 s / 258 tests | -323.73 s | 79.6% |

The full-suite time is truthful wall-clock evidence, not a CI performance gate.
The workload also became smaller because retired runtime behavior and its tests
were removed, so the 79.6% reduction must not be interpreted as a like-for-like
microbenchmark. The mutation benchmark is the like-for-like local ledger
comparison. Current targeted requirement projection is `0.002922 s` median;
the baseline did not record the same measurement, so it remains
`not-comparable`. Current full projection is `0.021977 s` median, or `7.52x`
the targeted projection cost.

Operational performance budgets pass, but the 35%-45% Python/test code target
does not. No delivery negative test was removed to improve the numbers, and no
format minification or migration/trust feature deletion is proposed as a false
fix. Explicit user approval is required by the locked exit rule before this
specific deviation can be accepted.

## Metric methods

- Baseline values are preserved in
  `docs/audits/2026-07-11-local-core-slimming-baseline.md` and in the benchmark
  report's immutable baseline section.
- Candidate and Plugin files: baseline Git-tree inventory; candidate tracked
  plus untracked filesystem inventory excluding the user-provided `openspec/`,
  `__pycache__`, and bytecode.
- Python LOC: `wc -l` over actual `.py` files under `kafa/`, `plugins/`,
  `tests/`, and `benchmarks/`, excluding generated caches and `.venv/`.
- Tables and indexes: fresh init followed by `sqlite_master`; indexes include
  SQLite autoindexes to preserve the baseline counting rule. The candidate has
  11 explicitly named indexes and 43 total indexes. The two additional
  autoindexes support database-enforced cycle/candidate scope on immutable
  execution-validation links.
- CLI nodes: recursive walk of every `argparse._SubParsersAction` choice from
  `harness.build_parser()`.
- Skills, Hooks, templates, schemas, and scripts remain locked by structure,
  feature-freeze, install, and release-contract tests. The delivered inventory
  is 7 Skills, 3 Hooks, 3 agent templates, 16 schemas, and 7 scripts.
- Plugin size: `du -sk` on a same-filesystem copy excluding `__pycache__` and
  `*.pyc`, matching the source payload rather than transient test artifacts.
- Database size, init, mutation, and projection values are five-sample medians
  produced by `benchmarks/run_local_core_benchmark.py`.
- Full test duration is the `/usr/bin/time -p` wall value; unittest's internal
  duration is recorded separately above.

The machine-readable evidence is
`docs/audits/2026-07-11-local-core-slimming-benchmark.json`. Its full-test entry
is explicitly `status=passed`, `test_count=258`, `seconds=82.99`.

## Architectural reduction

| Concern | Baseline | Candidate |
| --- | --- | --- |
| Host lifecycle | Kafa provider/worker/watchdog, dispatch, worktrees, model/Spark policy, receipts | Native host is sole lifecycle owner; Kafa accepts returned work and records local facts only |
| External systems | Connector profiles, tokens, SaaS adapters, outbox/recovery, advisory fallbacks | No business-runtime network clients or credentials; external Apps remain user-owned and outside Kafa facts |
| Task writes | Claims, leases, heartbeats, retries, fences, provider/reviewer writers | Root-controller-only `planned -> active -> submitted -> accepted`, plus fail-closed block/cancel paths |
| Verification | Mutable/copyable evidence and test rows plus receipt promotion | Insert-only execution facts linked to judgment-only validations |
| Audit | Whole-database snapshot/replay/checkpoint machinery | Compact append-only before/after audit events and targeted projections |
| Delivery trust | Connector/HMAC/CI and same-process receipt branches | Honest local states: `controller-verified`, `reviewed-local`, `same-context-degraded`, and `human-review-required` |
| High/critical risk | External provenance could promote trust | No autonomous pass without verifiable provenance; complete explicit risk acceptance is procedural and candidate/revision/expiry scoped |

## Native Host delegation and token audit

Kafa does not choose or store a model. The main Skill gives the Native Host
`fast`, `general`, and `deep` capability hints; the Host maps those hints to an
available model. The on-demand
`plugins/codex-project-harness/references/delegation-matrix.md` requires Task,
Acceptance, dependencies, exclusive/shared files, targeted/integration tests,
context/output budgets, and deterministic escalation. File overlap defaults to
serialization through one root integrator. The default is one producer/batch;
two or three require ready disjoint tasks, exact per-task and combined tests,
and a measured latency SLA saving after startup/integration/review cost. Unknown
cost or no SLA means no fan-out. Producer packets and output target 4,000 UTF-8
bytes; long logs stay in local artifacts and cohesive work is never split merely
to meet that target.

Default Skill-entry text fell from 60,862 baseline bytes across 12 Skills to
23,366 bytes across 7 Skills, a 61.6% reduction. `project-harness` itself is
12,544 bytes versus 11,653 baseline bytes; the 3,129-byte delegation matrix is
loaded only when work is actually delegated. The mandatory transitive context is
15,673 bytes, below its 16,000-byte regression limit. Token estimates based on
bytes are planning approximations, not Host billing telemetry.

Two opt-in real Native Codex profiles produced actual CLI token telemetry:

| Profile | Producers | Changed files | Host tokens | Controller wall time | Parallel overlap | Verification |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| Single producer | 1 | `candidate.py` | 51,266 | 21.916 s | n/a | 1 targeted controller verification |
| Parallel producers | 2 | `alpha.py`, `beta.py` | 102,107 total; 51,053.5 average | 22.406 s | 21.055 s | 2 targeted plus 1 combined verification |

The two parallel producer runtimes sum to 43.461 seconds; their measured overlap
reduced that producer wall window by 48.4% (`1.94x`). Because both profiles now
share the same canonical workload-unit digest, two sequential single units
project to 43.833 seconds versus 22.406 seconds in parallel, a 48.88% reduction
(`1.956x`). Tokens tell a different story: projected sequential usage is 102,532
versus 102,107 in parallel, only -0.41%, while fan-out still consumes 1.99x one
single invocation. That difference is noise-level evidence, not a token saving.
Parallelism improves latency; batching shared-context mechanical work is the
token-conservative route. The Host did not expose actual model identity or
pricing, so model A/B and monetary cost remain unmeasured rather than inferred.

Compact, source-bound evidence is stored in
`docs/runtime/native-codex-live-eval.json` and
`docs/runtime/native-codex-parallel-eval.json`; verbose Host output remains only
in temporary reports. Both reports share the same executable-source and Codex
binary hashes and record workload identity, declared/actual scopes, task states,
relative producer timing, structured token usage, and exact verification return
codes. The binary trust label is `local-capability-only-not-delivery-provenance`.
Login/version/exec use an auth-only, non-sensitive environment allowlist, and a
pre-output consistency gate independently recomputes summaries, live status,
source/binary identity, token aggregation, scope attribution, integration, and
timing.

The implementation exercise also exposed a real dispatch mistake: during one
review/fix loop, root and a worker were briefly authorized to edit
`tests/test_documentation_contract.py` concurrently. The patches happened in
different regions and both survived, but the authorization violated the new
shared-file rule. Root detected it during integration review, stopped parallel
writes to that file, and completed subsequent integration serially. This event
is not described as a conflict-free pass.

## Migration and rollback

- Fresh projects create schema 30 directly.
- Schema 29 converts side-by-side to schema 30.
- Published schema 27 and development schema 28 first use the isolated legacy
  conversion stage to schema 29, then the same schema 30 converter.
- Published schema 27 evidence is frozen from annotated tag
  `v1.21.3-beta.1` (commit `c5140a0`, source blob `3c13567`) rather than a
  relabelled synthetic fixture. The fixture asserts 53 tables, 60 indexes, and
  normalized DDL SHA-256
  `62c1046ed093ab3acdd1ceb22994b8c8c81242b26a997f5c2e77840e08b205f8`.
- The source database is backed up before destructive activation. Backup bytes,
  SHA-256 manifest, row counts, and migration facts are verified.
- Before activation, Kafa runs schema/FK/domain invariants and a full projection
  dry-run against an isolated copy of the staging database. Projection failure
  leaves the source active. After activation, projection rebuild plus final
  doctor execute inside the rollback boundary; either failure restores the
  verified backup atomically.
- Schema 30 contains exactly 27 approved tables; removed Connector/provider
  tables are not carried into the active database.
- Legacy task `failed` maps to `blocked`; legacy `skipped` maps to `cancelled`.
  Neither state is presented as accepted work.
- The real schema 27 stability scenario now performs both 27 -> 30 activation
  and post-replace rollback from the same authoritative 53-table fixture. It
  preserves requirement/task/execution/validation/decision facts and proves
  retired adapter/provider/snapshot/command-log sentinels stay out of active
  schema 30 while remaining recoverable in the backup.

## Delivery-trust audit

The executable negative matrix covers stale candidate, artifact digest mismatch,
failed/zero/malformed structured results, manual validation forgery, execution
overwrite/delete, same-context review, forged HMAC/CI-looking text, incomplete,
expired, or stale accepted risk, open high/critical findings, same-second gate
ordering, old-cycle isolation, dirty Git, unavailable sandbox, missing
no-network proof, event write rollback, and direct database tampering.

Executions and validations now expose composite `(id, cycle_id,
candidate_sha)` keys. `validation_executions` carries the same scope and uses
composite foreign keys, so a direct SQL link across cycle or candidate fails at
the database boundary instead of waiting for delivery evaluation.

High/critical active work without acceptable provenance remains
`human-review-required`. This candidate does not synthesize a receipt or turn a
manual claim into controller evidence.

## Adversarial review disposition

The main-model reviews covered the locked failure matrix plus the later
multi-agent/token simulation. Confirmed behavior defects were reproduced by red
tests before correction; prevention-only gaps received direct regression tests:

| Risk | Evidence and disposition |
| --- | --- |
| False delivery / stale candidate | `delivery record` previously re-read candidate state after validation without proving identity. It now captures the candidate before validation, checks it after validation and again before commit, stores only that validated identity, and rolls back on change. |
| Data loss / rollback | Projection rebuild was outside the migration rollback boundary. Staging projection dry-run and post-activation projection/doctor are now callback checks inside migration activation; injected failures prove `failed-before-activation` or `rolled-back` with schema 29/27 restored. |
| Cross-scope evidence forgery | A validation could be linked by direct SQL to an execution from another candidate. Composite scope foreign keys now reject that insert with `IntegrityError`. |
| Manual evidence / high-risk bypass | Judgment-only validations have no execution authority; current-candidate immutable execution is mandatory. High/critical work without complete explicit accepted/exempt risk remains `human-review-required`; context IDs are never cryptographic provenance. |
| Dirty tree / removed network | Delivery remains fail-closed after a gate if Git is dirty. Static runtime scans and structure tests found no SaaS endpoint, token, `gh api`, Host SDK import, worker, or provider execution path. |
| Maintenance-script side effect | `run_runtime_smoke.py --help` executed the smoke and rewrote a fixed report path; `run_skill_eval.py --help` also executed its fixture. Both now use `argparse`; a SHA-256 before/after check proves smoke help leaves the report unchanged, and `--out` makes writes explicit. |
| Single-writer template ambiguity | The project template named a retired `phase` update and role templates did not uniformly prohibit Kafa fact writes; QA could appear independent while editing the candidate. Root-only guidance now precedes state instructions, every role returns evidence, and QA is read-only with producer repair plus re-verification/re-review. |
| Fabricated Agent metrics | `task_once_completion_rate` equalled scenario pass rate and `retry_count` was hard-coded zero while token/runtime were null. The fabricated fields were removed; real Host tokens are parsed only when emitted and runtime is controller wall-clock. Unknown cost remains null. |
| Native model and secret ownership regression | Active runtime was clean, but tests did not prevent future model-selector or ambient-secret leakage. Runtime inventory rejects legacy selectors; login, version, and exec receive only copied auth plus a non-sensitive environment allowlist, and compact failures never echo Host stdout/stderr. |
| Native report forgery | Early reports trusted self-reported summary, live status, source hashes, scopes, token totals, and timings. The pre-output gate now recomputes them from scenarios, structured usage, actual checkout/binary bytes, producer diffs, and persisted timing windows; mutation probes fail closed. Binary identity remains explicitly local capability, not delivery provenance. |
| Parallel integration evidence | The old live profile proved one edit only. The new profile runs two isolated producers, derives attribution from exact diffs, blocks normalized overlaps/traversal, integrates deterministically through root, and runs two targeted plus one combined verification. Measured fan-out halves latency but uses essentially the same tokens per unit, so one/batch remains default without a latency SLA. |
| Context bloat | Inlining the full delegation matrix grew the always-loaded main Skill to 13,598 bytes. The main Skill is now 12,544 bytes and the 3,129-byte reference is on demand; their 15,673-byte transitive total is regression-capped at 16,000 bytes. Producer packets/output target 4,000 bytes without splitting cohesive work. |
| Scale-target truth | Recounting exposed that the prior 32.1% claim did not meet the locked 35%-45% target; the current truthful reductions are 28.6% total and 32.5% tests. An exact retained-surface call graph found all 101 `harness_db.py` functions reachable and only 4-12 unconditional production LOC; even compatibility-breaking candidates total only about 143 production/42 test LOC. There is no safe 1,811/327-LOC removal without deleting migration/rollback/trust coverage, so this remains an explicit approval-required deviation rather than a fabricated pass. |
| Simpler alternative | No second lifecycle, event replay system, or external trust adapter was reintroduced. The fixes reuse the existing side-by-side migration, SQLite foreign keys, and two validation callbacks; a broader coordinator or new persistence layer would add ownership and failure modes without improving the locked contract. |

The final adversarial matrix ran 91 tests with ResourceWarning promoted to
error and returned `OK`; no skip or expected failure was counted as passing.

## Retired-term classification

Retired identifiers are allowed only in the following reviewable contexts; a
positive production path does not qualify for any category.

| Category | Intentional locations | Disposition |
| --- | --- | --- |
| Current boundary statements | README, INSTALL, QUICKSTART, runtime docs, retained Skills, and templates | Negative statements explain that external systems and Host lifecycle are absent or Native-host-owned. |
| Static prevention | `kafa/cli.py`, `validate_structure.py`, the outer retired-command rejection in `harness.py`, and contract tests | Literals reject endpoints, tokens, SDK imports, retired commands, and removed files; they do not call them. |
| Migration-only compatibility | `schema_lifecycle.py`, `local_core_migration.py`, the registered 27/28/29 source guard in `harness_db.py`, and legacy fixtures | Isolated conversion reads old facts, filters retired entities, and emits schema 30 local facts; active schema 30 never creates those surfaces. |
| Historical record | CHANGELOG, dated audits, Superseded ADRs, historical schema 29 documentation, and the explicitly Superseded v1 operating-system plan | Preserved for audit provenance and clearly non-executable. |
| Repository maintenance | `.github/` workflows, release/tag metadata, and repository GitHub URLs | Maintains Kafa itself; it is outside business-project runtime facts and credentials. |

The active Plugin runtime has no SaaS endpoint call, Connector-token read,
`gh api` invocation, `openai_codex` import, provider worker, generic live-command
eval, or v1 compatibility wrapper. The task 10.8 exact search rechecked every
occurrence against this table; its additional HTTP/client/credential scan found
no active runtime call site.

## Task 10.8 checkpoint

- Documentation, release, freeze, local eval, and install contracts: 71/71,
  `OK`, with no skipped result.
- Release contract JSON: `ok=true`, state `development`, Runtime/Kernel 5.0.0,
  schema 30.
- Plugin structure and strict non-interactive OpenSpec validation: passed.
- Isolated source-to-wheel installation: exact 7 Skills, 3 Hooks, 3 templates,
  7 runtime scripts, 16 schemas, cache discovery, schema 30 init, installed
  quickstart verification, doctor, and uninstall all passed.
- Retired-term search: every hit classified above; active runtime external
  endpoint/client/credential inventory remained empty.

## Final Wave 11 evidence

- `py_compile`: passed for the root package, benchmarks, Plugin core/scripts,
  Hooks, Skill proxy, and all tests, with bytecode redirected outside the source
  tree.
- Plugin structure and strict non-interactive OpenSpec validation: passed.
- Strict primary regression: 258/258 in 82.99 wall seconds, `OK`, no skip.
- Runtime smoke: 2/2; Skill eval: 17/17 required markers; local fixture E2E:
  6/6; local stability E2E: 11/11. False-pass, skipped, SQLite-lock, and human
  intervention counts were all zero.
- Schema/migration/rollback matrix: 31/31, including authoritative schema 27,
  schema 28/29, projection dry-run failure, post-activation doctor failure,
  atomic restore, backup integrity, and admin recovery.
- Source-to-wheel and clean source-archive artifact installs both returned
  `ok=true`: exact 7 Skills, 3 Hooks, 3 templates, 7 runtime scripts, 16 schemas,
  cache digest/discovery, schema 30 init, installed quickstart stopping at
  `submitted`, doctor, uninstall, and retired-runtime absence all passed.
- Real Native Codex single profile: `live_status=passed`, 1/1 scenario, 51,266
  tokens and 21.916 controller-wall seconds. The Host changed only
  `candidate.py`; controller verification returned 0; execution/validation
  counts were 1/1; task status was `submitted`; provider surface was absent.
- Real Native Codex parallel profile: `live_status=passed`, 1/1 scenario. Two
  producers changed only `alpha.py` and `beta.py`, overlapped for 21.055
  seconds, consumed 102,107 total tokens, passed two targeted verifications and
  one combined verification, and left the integration task `submitted` after
  both producer tasks were accepted. `scope_conflicts={}` and
  `retired_host_tables=[]`.
- Adversarial negative matrix: 91/91 after the simulation findings were
  corrected and converted to regression contracts.
- A final focused distribution/runtime contract rerun passed 57/57 in 20.243
  seconds with ResourceWarning promoted to error. Its only workspace side effect
  was Python bytecode cache creation; the generated caches were removed and the
  pre/post `git status --porcelain` SHA-256 returned to
  `6a7877ecd597cb9097778bd3cd4ca0adc90a0da3bfd0f612b971b3d023e53531`.
- Exact retained-surface audit: 53 CLI parser nodes remain within the locked
  domains; all 101 `harness_db.py` top-level functions are reachable, including
  two dynamic call sites. The schema 27/28/29 conversion block remains an active
  schema-30 migration dependency, not dead runtime.
- User decision: on 2026-07-12 the user explicitly accepted the measured
  28.6% total-Python and 32.5% test-Python reductions as a documented deviation
  from the locked 35%-45% target and authorized task 11.16 to close. The raw
  metrics and shortfall remain visible rather than being reported as a pass.
- Operational scale budgets pass and default Skill-entry bytes fell 61.6%.
  Total/test/Plugin Python reductions are 28.6%/32.5%/31.3%; the locked
  35%-45% total/test code target remains approval-required rather than passed.
- Final scope audit: branch `v2-local-core-slimming`; `HEAD`, `main`, and
  `origin/main` all remain at
  `adba3691d859c0ffc93d75cc148d8f916314cc49`; cached diff is empty;
  `git diff --check` passes; no source-tree bytecode, build, wheel, archive,
  database, backup, or egg-info residue remains. The eight user-provided
  `openspec/` files are retained. No commit, push, merge, tag, release, or
  deploy was performed.

## Residual risks and boundary

- The locked total/test code target is not met. Exact-surface analysis found only
  4-12 unconditional production LOC, or about 143 production/42 test LOC if
  optional public compatibility is also broken. Neither closes the 1,811/327 LOC
  gap, and larger deletion risks supported migration, rollback, or trust
  coverage. The user explicitly accepted this residual scale deviation on
  2026-07-12; it remains a recorded limitation, not a passing metric.
- Local context identifiers and explicit risk acceptance are procedural audit
  metadata, not cryptographic provenance. This limitation is surfaced as
  `human-review-required` for unresolved high/critical work rather than hidden.
- The authoritative published schema 27 fixture is frozen to
  `v1.21.3-beta.1`; unknown, older unsupported, or newer schemas fail closed.
- The strict 258-test regression and both real Native Codex profiles ran on the
  primary macOS platform. Three-platform workflow contracts were validated
  locally, but no remote CI run exists because the user prohibited push and
  release operations.
- The source candidate is `2.0.0-beta.1`/schema 30, but the machine's enabled
  `codex-project-harness@personal` and global `kafa` remain
  `1.25.0-beta.1`. Refreshing the effective installation is an installation or
  deployment action and was not performed without explicit authorization. The
  candidate source doctor passes its local-only/control-plane checks but reports
  the expected missing project-local marketplace; the old global doctor still
  checks retired v1 surfaces and is not candidate-source evidence.
- Native Host telemetry exposes token counts and controller wall time, but not
  a trustworthy actual-model identity or price. The capability-hint routing
  contract is verified; cross-model quality/cost A/B remains unmeasured and is
  not claimed.
- The working tree is intentionally uncommitted. Publishing, version-control
  integration, release, and deployment remain outside this task until the user
  explicitly authorizes them.

No locked architecture decision was changed. The sole unmet quantitative target
is the measured code-scale deviation above; the user explicitly accepted it and
authorized task 11.16 to close without weakening migration, rollback, or trust
coverage.

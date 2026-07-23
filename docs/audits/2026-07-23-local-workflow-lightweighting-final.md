# Kafa Local Workflow Lightweighting Final Audit

Date: 2026-07-23
Change: `local-workflow-lightweighting`
Branch: `v2-local-workflow-lightweighting`
Base HEAD: `7c7aa41929426bc1d89350497ceb2c9266290b88`
Delivery state: local uncommitted candidate; no release or deployment

## Decision

The allowed local scope is complete. P0 created one workflow presentation
source, an atomic delivery-plan entrypoint, an immutable verified-patch
entrypoint, concise operator output, and fact-derived delivery narrative. P1
normalized the public project entrypoint, distribution inventory, and advanced
triggers. P2 scoped release evidence, unified artifact identity, reduced stable
evidence summaries, and made absent field metrics compact without fabricating
zeros.

The change preserves schema 31, 30 product tables, local-only runtime,
root-controller single-writer task state, Native Host lifecycle ownership,
immutable execution, current-candidate verification, medium/high trust, and the
canonical fail-closed delivery prerequisite evaluator. It adds no Connector,
remote business API, Host SDK worker, second lifecycle, release, deployment,
production migration, or user-plugin replacement.

All final independent-QA Critical/High/Medium findings are closed. No final
Critical, High, Medium, test failure, or test error remains in the allowed local
scope.

## Red/Green And Finding Closure

| Contract/finding | Red evidence | Green/closure evidence |
| --- | --- | --- |
| LWL-P0-1 single workflow source | Independent workflow lists and generated-view drift were possible; delivery-plan/output/narrative suites were still intentionally red at baseline. | Versioned closed contract, deterministic renderer, generated/checkable views, and P0 combined gate 327/327. |
| LWL-P0-2 atomic plan and verified patch | Initial delivery-plan/verified-patch contract had 13 failed assertions across 11 tests. | One `BEGIN IMMEDIATE`, exact no-op replay, rollback/recovery tests, immutable `verify_run()` reuse, and final plan contract 24/24. |
| LWL-P0-3 concise output | 10 tests produced 20 failed assertions against noisy or incomplete output behavior. | Shared state/blocker/action envelope, complete verbose/JSON modes, recovery-first behavior, and affected presentation gate 88/88. |
| LWL-P0-4 derived narrative | Initial eight-test contract produced one pass, four failures, and three errors. | Structured relation authority, supplemental-only prose, historical fact derivation, and final narrative contract 29/29. |
| LWL-P1 exit findings | Independent QA reported 4 High and 12 Medium findings across public doctor, private runtime execution, inventory, Hook/App Server wiring, and evaluator truth. | All `LWL-P1-F1` through `LWL-P1-F16` closed; affected combined gate 229/229 and two bounded re-reviews 58/58 and 79/79. |
| LWL-P2 exit findings | Independent QA reported 1 High and 6 Medium findings across scope classification, summary truth, supply-chain reads, rehearsal/outcome validation, and private evaluator snapshots. | All `LWL-P2-F1` through `LWL-P2-F7` closed; affected gate 48/48 and combined P2 gate 209/209. |
| LWL-FINAL-F1 generated happy path | Deterministic reproduction showed `T1`/`Q1` did not equal generated `PATCH-T1`/`PATCH-Q1`, and a planned task could not be submitted without start. | Canonical `task-start`, exact generated IDs, and a real temporary project executing every generated command through delivery validation. |
| LWL-FINAL-F2 partial SQLite JSON | Three commands returned nonzero with empty stdout and traceback for a readable but incomplete schema. | `status --json`, `doctor --json`, and `quickstart status --json` now return exactly one fail-closed error envelope, no traceback, no init action. |
| LWL-FINAL-F3 baseline ordering | The presentation graph accepted readiness before baseline confirmation. | Direct `baseline-confirmation -> delivery-readiness` edge plus inverse-order rejection test. |

The final three QA contracts failed before production edits, then passed 4/4.
Their affected workflow, docs, output, cold-start, delivery-plan, local-core,
Hook, and feature-freeze gate passed 135/135. Bounded re-review passed 28/28
with 0 open Critical/High/Medium findings.

## Exact Test Accounting

Counts below overlap by design and are not added into one synthetic total.

| Evidence | Result | Failure/error | Skip/expected failure | Truth label |
| --- | ---: | ---: | ---: | --- |
| Final complete unittest discovery | 960 pass / 974 run | 0 / 0 | 14 / 0 | passed with explicit skips |
| Final-QA affected gate | 135/135 | 0 | 0 | passed |
| Schema 31, 30-table, migration/backup/rollback gate | 140/140 | 0 | 0 | passed |
| Local-only/single-writer/Host ownership/execution/trust/gate gate | 220/220 | 0 | 0 | passed |
| No external runtime/second lifecycle/automatic action boundary gate | 93/93 | 0 | 0 | passed |
| Final QA A initial read-only suites | 259/259 | 0 | 0 | tests passed; review still found 1 High and 2 Medium, later closed |
| Final QA B read-only suites | 286/286 | 0 | 0 | passed; 0 open Critical/High/Medium |
| QA A bounded re-review | 28/28 | 0 | 0 | passed; 0 open Critical/High/Medium |
| Runtime smoke | 2/2 | 0 | 0 | real local runtime smoke |
| Fixture E2E | 6/6 | 0 | 0 | fixture-only, not Host evidence |
| Stability E2E | 11/11 | 0 | 0 | deterministic-local-runtime, not Host evidence |
| Skill eval | 39/39 required markers | 0 | 0 | fixture-only, not fresh Host evaluation |
| Artifact/release/supply-chain regression | 91/91 | 0 | 0 | passed |
| Real wheel/sdist LICENSE contract | 3/3 | 0 | 0 | passed with artifact paths injected |
| Delivery-integrity outcome benchmark | 4/4 scenarios | 0 | 0 | passed; `field_metrics_status=not-observed` |

The 14 discovery skips are not passes:

- 12 macOS skips are Windows-only filesystem/path contracts in
  `tests/test_project_fs_safety.py`;
- 2 discovery skips are artifact LICENSE checks because discovery did not set
  `KAFA_TEST_WHEEL`/`KAFA_TEST_SDIST`; the later dedicated real-artifact run
  passed 3/3 and is reported separately.

The first exact offline build probe used `--no-build-isolation` from the system
Python and failed because that interpreter did not contain Setuptools. It is a
tooling invocation failure, not a product test pass or failure. A provisional
`uv build` artifact pair was also discarded because its actual command did not
match the fixed builder command that provenance would declare. Final artifacts
were built and evidenced in one pinned isolated environment using the exact
declared `python -m build --no-isolation --wheel --sdist --outdir ...` command.

## Runtime, Structure, And Compatibility

- Runtime/kernel/schema: `5.0.0` / `5.0.0` / schema 31.
- Product tables: exactly 30; fresh DB: 380,928 bytes.
- Public parser nodes: 61 (baseline 59; the two additions are
  `delivery-plan` and `verified-patch`).
- Distribution: 7 Skills, 3 Hook events, 3 Native agent templates, 18 schemas,
  20 core modules, 7 scripts, 3 references, and 22 public runtime domains.
- Renderer `--check`, plugin structure, repository JSON parsing, schema
  contracts, documentation contract, and `git diff --check` passed.
- Schema 27/28/29/30 migration and rollback coverage remains compatible;
  migration invents no qualification, gate, outcome, or narrative authority.
- Forbidden runtime scans found only negative guard constants and legacy
  migration de-trusting logic; added process execution is local project
  delegation, read-only Git, or `codex plugin list` inspection.

## Lightweighting Before/After

| Metric | Baseline | Final | Result |
| --- | ---: | ---: | --- |
| Independently maintained workflow lists | 7 | 1 | -85.714% |
| Seven physical guidance files | 69,620 B | 54,803 B | -14,817 B / -21.283% |
| Conservative maintained guidance | 69,620 B | 41,769 B | -27,851 B / -40.004%; `>=40%` target passed |
| Entry Skill | 12,711 B | 12,759 B | `<=12,800` passed |
| Entry plus required default references | 12,711 B | 12,759 B | zero default reference bytes; `<=16,000` passed |
| Triggered entry plus delegation matrix | 14,303 B | 15,905 B | advanced-only; `<=16,000` passed |
| Plan/setup actions | 10 | 3 | apply plan, confirm baseline/scope, verify patch |
| Plan graph transaction | multiple writes | one `BEGIN IMMEDIATE` | exact replay writes nothing |
| Initialized-empty quickstart | 3,392 B / 23 lines / 8 actions | 410 B / 3 lines / 1 action | -87.913%; `<=848 B` passed |
| Initialized status | 192 B / 11 lines | 410 B / 3 lines / 1 action | byte increase; concise-card contract passed |
| Healthy doctor | 26 B / 1 line | 40 B / 3 lines / 0 actions | byte increase; concise-card contract passed |
| Main five implementation surfaces | 10,166 LOC | 14,491 LOC | +4,325 / +42.544%; explicit deviation, not lightweight LOC |
| Source plugin payload | 71 files / 1,333,527 B | 75 files / 1,500,474 B | +166,947 B / +12.519%; old 1 MiB budget remains exceeded |
| Fresh DB | 380,928 B | 380,928 B | unchanged; historical 320 KiB deviation remains |

The maintained-guidance measurement is 29,841 bytes of manual remainder plus
one 11,928-byte workflow contract. Generated blocks remain readable physical
views, not independent policy sources. The Host exposes no trustworthy isolated
Skill-token counter, so byte counts are reported instead of invented token
estimates.

## Performance

Wall-clock values are comparative evidence except for the established 50 ms
mutation/plan-apply budgets.

| Metric | Final measurement | Status |
| --- | ---: | --- |
| Fresh init, 5-sample median | 0.183858 s | comparative |
| 5k-fact mutation, 5-sample median | 0.019855 s | passed `<=0.050 s` |
| Three-view projection, 5-sample median | 0.008260 s | comparative |
| Full 13-view projection, 5-sample median | 0.028628 s | comparative |
| 5k-fact delivery-plan apply, 7-sample median | 0.023873 s | passed `<=0.050 s`; every first apply changed=true |
| Exact plan replay, 7-sample median | 0.009397 s | comparative; every replay changed=false |
| Cold source CLI help, 11-sample median | 0.065310 s | comparative |
| Cold initialized status, 11-sample median | 0.205097 s | comparative |
| Warm deliverable evaluator, 21-sample median | 0.031117 s | 0 blockers, delivery allowed, proven AC1 |

The local-core benchmark's embedded `full_test` field remains `not-run`; the
separately completed 974-test discovery is not injected into that field or
mislabelled as the benchmark's own run.

## Artifacts, Install, And Supply Chain

Final artifacts were built from the stable package/plugin inputs with pinned
`build==1.5.0`, `setuptools==83.0.0`, and checksum-verified Syft 1.48.0
(`3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6`).

| Artifact | Baseline | Final | Delta |
| --- | --- | --- | ---: |
| Wheel | 48,085 B | 81,025 B / `bf8926fa56da7dc6bb9506d309496fb80fbc756de6cfa1b6baa5fe648053e4c1` | +32,940 B / +68.504% |
| Source archive | 502,068 B | 612,968 B / `798e71362462e470ee44624b6973bb6d282f3ee2d4b67790dde423094bd2fc53` | +110,900 B / +22.089% |
| Combined | 550,153 B | 693,993 B | +143,840 B / +26.145% |

Artifact growth is an explicit deviation, not a lightweighting pass. No trust,
recovery, migration, path, or evidence check was deleted to reduce it.

The exact pair passed isolated venv + isolated HOME installation with
`artifact_mode=true` and `ok=true`: wheel import, marketplace registration,
Codex App Server discovery, 7 Skills, 3 Hooks, 3 templates, 18 schemas, 20 core
modules, 7 scripts, 3 references, 22 domains, quickstart, direct/cache Hook,
schema-30 dry-run, schema-30-to-31 migration, backup, doctor, uninstall, cache
removal, and marketplace cleanup. Source/managed/cache plugin tree SHA-256 was
`76a513fd2849ee50ec1657fbca4471316cfb5495c8a1fa114c565b067fe2e715`.
The test project migration backup SHA-256 was
`0c482176264b715369c04242dfeb65d2f9e2ec140ad23f2ce1965b93dd544ecd`;
the manifest SHA-256 was
`9fd66163f9f5c5192adfe77cc7e8a30503887cf4836ef83e2d1198184249c7ef`.
Only ephemeral test state was migrated.

Supply-chain generation and independent verification passed for exactly two
artifacts and two CycloneDX 1.6 SBOMs, `SHA256SUMS`, and an in-toto/SLSA local
provenance statement. Assurance is truthfully
`unsigned-local-integrity-statement`; this is not a published attestation or
release. The provenance binds the exact pre-audit build snapshot. Subsequent
changes are limited to this final audit and OpenSpec checklist evidence, which
are not package/plugin inputs; the statement is not relabelled as current
release provenance for the final dirty worktree.

Stable Native summary files are 2,331 bytes versus 10,448 bytes of retained
detail, a 77.690% review-surface reduction. The no-field-window outcome report
is 8,280 bytes versus 10,080 bytes, a 17.857% reduction, and reports only
`field_metrics_status=not-observed`, not fabricated zero metrics.

## Independent QA And Four-Angle Review

- **Logic gaps:** the final QA exposed and closed generated-ID/task-start,
  incomplete-SQLite, and baseline-ordering gaps. A real generated command chain
  reaches delivery validation; delivery-plan still creates no lifecycle/gate/
  delivery fact itself.
- **Incorrect facts:** final artifact provenance now declares the command that
  actually built the artifacts; the provisional mismatched-command pair was
  discarded. Fixture, historical, skip, and not-run states remain separately
  labelled.
- **Simpler alternatives:** no new schema, lifecycle, table, or duplicated
  workflow list was needed. Fixed example IDs and one bounded expected-state
  exception tuple close the findings more simply than dynamic prose parsing or
  a second recovery path.
- **Proof sufficiency:** deterministic red/green, 135/135 affected tests,
  974-test discovery, two independent QA axes, bounded re-review, real
  artifacts, isolated install, real SBOM/provenance verification, runtime smoke,
  fixture/stability E2E, and performance measurements cover the allowed scope.

Two low operational trade-offs remain: projection publication follows the
atomic fact commit and may require supported rebuild after a publication error;
the three-line default intentionally hides lower-priority blockers that remain
available in verbose/JSON.

## Explicit Skip, Fixture, Historical, And Not-Run States

- Fresh real Native single/parallel profiles for the final source:
  `not-run — excluded by user; residual risk accepted`.
- Remote Ubuntu/macOS/Windows CI:
  `not-run — excluded by user; residual risk accepted`.
- Authenticated Host Hook invocation:
  `not-run — excluded by user; residual risk accepted`; deterministic App
  Server discovery and direct/cache Hook execution are not substitutes.
- The retained Native single/parallel reports are `state=historical`; their
  detail integrity and summaries validate, but they do not prove the final
  candidate current-eligible.
- Default Skill eval is fixture-only; fixture/stability E2E are local
  deterministic evidence, not fresh Host evidence.
- Commit, push, merge, tag, release, deploy, production migration, and global
  user-plugin replacement are not-run. No item is described as passed.

Accepted residual risk is limited to the user-excluded fresh Native/remote-CI/
authenticated-Host observations and the two Low local trade-offs above. The
dirty source state is intentional uncommitted work and the local provenance is
unsigned; neither is represented as a release candidate or published supply-
chain attestation.

## Final OpenSpec State

After the dated audit and task evidence were written:

- `openspec status --change local-workflow-lightweighting` reported the
  spec-driven change at 4/4 artifacts complete (`proposal`, `design`, `specs`,
  and `tasks`);
- `openspec validate local-workflow-lightweighting` reported
  `Change 'local-workflow-lightweighting' is valid` with exit code 0.

Both commands were then repeated after this evidence update. No pending result
is counted as passed.

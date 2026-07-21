# Delivery Integrity Hardening Baseline

Date: 2026-07-20

## Authority And Scope

- Base: `main@e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb`
- Remote: `origin/main@e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb`
- OpenSpec: `openspec/changes/delivery-integrity-hardening/`
- Issue authority: `docs/audits/2026-07-20-delivery-integrity-issue-checklist.md`
- Release facts: `2.0.0-beta.1`, Runtime/Kernel `5.0.0`, schema `30`,
  `release_state=development`
- Worktree at measurement: dirty only because the confirmed issue checklist and
  new OpenSpec planning artifacts are untracked; no production runtime/schema
  file had been edited when the baseline tests and benchmark ran.

This baseline is the pre-production-edit checkpoint required by task 2.1. It
does not claim the audited P0 behaviors are safe: the existing regression suite
was green while the separately reproduced false-delivery paths still existed.

## Frozen Surface

| Contract | Current baseline |
| --- | ---: |
| Active product tables | 27 |
| Declared SQLite internal tables | 1 (`sqlite_sequence`) |
| Recursive CLI parser nodes | 53 |
| Public JSON schemas | 16 |
| Skills | 7 |
| Hooks | 3 |
| Native agent templates | 3 |
| Generated projection paths | 13 |
| Python source files (`kafa/`, `plugins/`, `tests/`, `benchmarks/`) | 66 |
| Total Python physical LOC | 51,725 |
| Plugin Python physical LOC | 25,503 |
| Test Python physical LOC | 23,774 |

Fresh schema-30 product tables:

```text
acceptance
baselines
decisions
deliveries
delivery_acceptance
delivery_cycles
events
executions
failure_mode_acceptance
failure_modes
findings
invalidations
migrations
project
quality_gate_findings
quality_gates
requirement_acceptance
requirements
task_acceptance
task_dependencies
task_failure_modes
task_test_targets
tasks
test_targets
validation_executions
validation_failure_modes
validations
```

## Strict Full Regression

Command:

```bash
/usr/bin/time -p python3 -B -W error::ResourceWarning \
  -m unittest discover -s tests -p 'test_*.py'
```

Result:

| Fact | Result |
| --- | ---: |
| Tests reported by unittest | 571 |
| Skipped | 12 |
| Failure/error | 0 |
| Non-skipped successful tests | 559 |
| unittest internal duration | 206.667 s |
| Wall time | 207.15 s |
| User time | 131.31 s |
| System time | 58.15 s |

The 12 skips remain skips and are not included in the 559 non-skipped success
count. This result is a baseline regression fact, not evidence that the new P0
contracts already exist.

## Performance And Size

The 5,000-fact benchmark used five samples and injected the exact full-suite
count and internal duration above:

```bash
python3 -B benchmarks/run_local_core_benchmark.py \
  --facts 5000 \
  --samples 5 \
  --out /tmp/kafa-delivery-integrity-baseline.json \
  --test-duration-seconds 206.667 \
  --test-count 571 \
  --test-status passed
```

| Metric | Current baseline | Existing budget/status |
| --- | ---: | --- |
| Fresh DB | 315,392 B | <=320 KiB, passed |
| Plugin payload, caches excluded | 1,044,089 file B | <=1 MiB, 4,487 B headroom |
| Plugin copy allocated size | 1,208 KiB | comparative only; file-byte budget is canonical |
| Fresh init median | 0.159367 s | measured |
| One mutation after 5,000 facts | 0.017853 s | <=0.050 s, passed |
| Full 13-view projection median | 0.067490 s | measured |
| Targeted 3-view projection median | 0.013683 s | measured |
| Strict full suite | 571 total / 12 skipped / 206.667 s | baseline passed with skips distinct |

Raw timing evidence is temporary at
`/tmp/kafa-delivery-integrity-baseline.json`. The values needed for future
comparison are preserved here so deletion of a temporary file cannot erase the
baseline.

## Baseline Methods

- Database inventory and bytes: fresh CLI `init`, then `sqlite_master` and file
  stat on the generated database.
- CLI nodes: recursive walk over every `argparse._SubParsersAction` choice from
  `harness.build_parser()`.
- Plugin payload: sum of regular-file bytes from a same-filesystem copy,
  excluding `__pycache__` and `*.pyc`; allocated size is reported separately.
- LOC: `wc -l` over actual Python files, excluding bytecode caches.
- Timing: medians from `benchmarks/run_local_core_benchmark.py`; wall-clock
  values are comparative evidence, while only declared budgets are pass/fail
  gates.
- Full-suite count: unittest output; skipped, expected-failure, blocked,
  fixture-only, and not-run are never reclassified as passing.

## P0 Red Contract Checkpoint

No production runtime, schema, CLI, projection, or migration file was changed
before this checkpoint. The new contract suite is
`tests/test_delivery_integrity_p0_contracts.py`; its fixtures use only temporary
project roots. A test-local compatibility table lets schema 30 reach the
qualification assertions without pretending the production schema already has
those facts.

Command:

```bash
python3 -B -W error::ResourceWarning -m unittest \
  tests.test_delivery_integrity_p0_contracts
```

Unmodified schema-30 result:

| Fact | Result |
| --- | ---: |
| Test methods run | 18 |
| Expected contract failures, including subtests | 35 |
| Fixture/setup errors | 0 |
| Skips / expected failures | 0 / 0 |
| Positive cancellation controls already passing | 2 |
| Internal duration | 11.559 s |

The failures are intentionally reported as failures, not passing tests. They
separate current false-positive behavior from missing future contract surfaces:

| Contract family | Expected red evidence |
| --- | --- |
| Empty minimum graph | direct API did not raise; CLI validation returned 0; CLI record returned 0 and wrote delivery |
| One-at-a-time graph gaps | 9 subtests failed because the structured evaluator and canonical codes do not exist yet |
| Cancelled sole coverage | trace returned no issue, delivery validation returned 0, and direct record did not raise |
| Positive cancellation controls | unrelated cancelled history and accepted replacement coverage both remained deliverable |
| Qualification required before execution | the expired-card acceptance accepted an unrelated arithmetic execution and wrote evidence |
| Qualification currentness | acceptance revision and 8 execution-relevant target mutations were ignored or failed only after reaching another policy check |
| Cross-acceptance qualification | an AC1 mapping could be bypassed by attaching the same arithmetic target to AC2 |
| Exact gate review | a passing gate with no qualification link still allowed delivery |
| Public qualification/gate-link surface | `test-target qualify` and repeatable gate qualification input do not exist yet |
| Shared blocker surfaces | the structured evaluator, quickstart blocker objects, and canonical CLI code rendering do not exist yet |
| Readiness and consistency modes | public readiness, non-circular enter mode, record-phase enforcement, and delivered consistency do not exist yet |

The first run exposed one test-fixture projection mismatch after a direct phase
mutation. The fixture was corrected to rebuild projections, then the complete
suite was rerun. The checkpoint above is the rerun: 35 expected contract
failures and zero setup errors.

An independent read-only test-design review confirmed the four reported bypass
roots. It also found that the original design had not enumerated blocker codes
and could let a new plain baseline borrow an older scope confirmation. Before
production edits, the OpenSpec design/spec were tightened to lock the P0 code
catalog and require the latest baseline confirmation identity; a later plain
freeze makes scope unconfirmed. `openspec validate
delivery-integrity-hardening` then passed.

## Initial Risk Statement

The current runtime demonstrably lacks the new minimum-graph,
cancelled-coverage, qualification, and shared-prerequisite contracts. The red
checkpoint is now complete, so production work may proceed in the frozen task
order beginning with schema 31 and recoverable migration tests.

## Schema 31 And Migration Red Checkpoint

Before editing schema or migration production code, the deterministic suite
`tests/test_schema31_contract_and_migration.py` was added and run with:

```bash
python3 -B -W error::ResourceWarning -m unittest -v \
  tests.test_schema31_contract_and_migration
```

Unmodified schema-30 result:

| Fact | Result |
| --- | ---: |
| Test methods run | 14 |
| Expected contract failures, including state subtests | 16 |
| Fixture/setup errors | 0 |
| Skips / expected failures | 0 / 0 |
| Internal duration | 0.703 s |

The failures separately prove the absent schema-31 factory/catalog, unsupported
30-to-31 CLI path, missing generation-neutral staging/activation APIs, missing
legacy-state preflight/normalization, and the not-yet-exercisable schema-31
rollback/recovery matrix. The latter already encodes exact database/projection
bytes and modes, manifest states, sentinel fail-closed behavior, and process
hard-exit assertions; missing APIs are explicit assertion failures rather than
import or setup errors.

## P0 Green Checkpoint

The schema-31 migration foundation and P0 delivery contracts are implemented on
the uncommitted local candidate. No commit, push, merge, release, deployment,
user-install replacement, or business-project migration occurred. The current
local content candidate at this checkpoint is:

```text
7bfc0204962da7afd9081c26c01ada672d0be5079fdb4dc2c12f4f2f2618bc71
```

The focused contract suite grew from the original 18 red methods to 30 green
methods. It now includes duplicate/blank/cross-cycle qualification inputs,
same-second Q9 -> Q10 supersession, target revert, repeatable exact gate links,
judgment-only trace exclusion, baseline/readiness atomicity, all three candidate
change boundaries, proxy help, projection gate status, and a public CLI-only
delivery journey.

```bash
python3 -B -W error::ResourceWarning -m unittest \
  tests.test_delivery_integrity_p0_contracts
```

Result: 30/30 passed in 23.634 seconds; zero failures, errors, skips, expected
failures, or unexpected successes.

The schema lifecycle/migration/rollback checkpoint remains 119/119 passed in
20.299 seconds. The broader P0 regression command covered delivery policy,
delivery cycles, traceability, task lifecycle, quickstart, CLI, proxy,
projection, schema 31, and execution validation:

```bash
python3 -B -W error::ResourceWarning -m unittest \
  tests.test_delivery_integrity_p0_contracts \
  tests.test_delivery_cycles \
  tests.test_harness_runtime \
  tests.test_execution_validation \
  tests.test_single_writer_tasks \
  tests.test_cold_start_guided_loop \
  tests.test_feature_freeze \
  tests.test_local_delivery_policy
```

Result: 125/125 passed in 78.015 seconds; zero failures, errors, skips, expected
failures, or unexpected successes. `git diff --check` passed at the checkpoint.

An early full-discovery compatibility probe was also run and is deliberately
not reported as passing: 618 total, 34 failures, 2 errors, and 12 skips. The
remaining failures are dominated by schema-30 fixture assumptions, pre-
qualification verify calls, outdated fixture/E2E table inventories, and schema
30 documentation/release assertions. They remain required work for the P0 exit
gate and later P1 documentation/E2E tasks; the 12 skips remain skips.

The principal P0 residual boundary is explicit: qualification proves that a
named controller procedurally bound one acceptance revision to one target
definition digest. It does not infer business semantics and is not
cryptographic identity or external provenance. Exact latest qualification,
current execution, accepted task, current gate review, baseline confirmation,
readiness, and delivered consistency are still re-evaluated independently.

## P0 Stop-Ship Exit Checkpoint

The P0 exit gate completed on the final P0 executable-source identity:

```text
workspace_sha256 2719f07036be10e7a7931b83bc9cd2110520eb37201941593588dcce9fc941e2
status_sha256    eae79170b8e04a2b6173060376b357b849fa2df6850b1c5ea9635139bb867f1f
git_head         e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb
git_dirty        true (intentional uncommitted implementation)
```

No commit, push, merge, tag, release, deploy, user-install replacement, or
business-project migration occurred.

### Test and E2E evidence

- The final full command was
  `python3 -B -W error::ResourceWarning -m unittest discover -s tests -p
  'test_*.py'`: 623 total in 292.914 seconds, 611 passed, 12 skipped by explicit
  capability/platform conditions, zero failures, errors, expected failures, or
  unexpected successes. The 12 skips are not counted as passes.
- The post-review migration/delivery critical set ran 134/134 in 65.009
  seconds with ResourceWarning as error.
- Runtime smoke passed 2/2. Deterministic fixture E2E passed 6/6 in 7.688
  seconds; stability E2E passed 11/11 in 12.459 seconds. Both reported zero
  skip, false-pass, or human-intervention counts.
- The four regression outcomes that were false-delivery before hardening all
  failed closed after hardening: empty direct API graph, sole cancelled task,
  unrelated unqualified target, and low-level record before readiness. The
  focused outcome run passed 4/4 in 3.192 seconds. This is a deterministic
  regression outcome, not a field-adoption metric.
- The schema-30 copy was exercised through side-effect-free dry-run, real
  30-to-31 migration, every injected rollback boundary, and a public
  post-migration baseline-confirm -> qualify -> verify -> gate -> ready ->
  record journey under isolated HOME. Invalid requirement, acceptance, and
  failure-mode states now fail the dry-run before backup or activation.
- Plugin structure passed; 28 JSON files parsed with zero failures; release
  validation returned `ok=true`; OpenSpec status reported 4/4 artifacts and
  validation passed; `git diff --check` passed.

### Adversarial review and closure

The main-model review checked graph closure, direct API paths, qualification
supersession, cancelled coverage, candidate races, readiness circularity, and
migration rollback. Two independent read-only QA reviews then found:

1. **Critical:** `cycle close --status delivered` could close an empty cycle
   without a delivery row. A deterministic API/CLI red test reproduced it;
   the API now rejects that status before mutation and directs callers to
   `delivery record`. Independent re-review reproduced nonzero CLI return,
   active cycle status, and zero deliveries.
2. **High:** migration dry-run only checked the version path. Red tests proved
   three invalid schema-30 state domains passed dry-run. Dry-run now holds the
   operation lock, performs the real staging conversion and projection
   validator, returns conversion counts, removes the staging DB, and leaves
   active DB/projections/backup/sentinel untouched. Independent re-review found
   no remaining Critical, High, or Medium issue.

The red checkpoint for these review findings produced one direct-close failure
plus three invalid-state subtest failures; the immediate green checkpoint was
3/3, followed by the 134-test critical set and complete discovery above.

### Current Native Host evidence

The user explicitly authorized sending only synthetic task prompts and
temporary test files to chatgpt.com. Current compact reports bind the exact P0
identity above:

- single: 1/1 passed, 35.511664 seconds Native runtime, 51,874 tokens (39,424
  cached input), one allowed `candidate.py` change, controller verification
  passed;
- parallel: 1/1 scenario passed with two producers, 52.337389 seconds Native
  runtime, 47.207348 seconds measured overlap, 118,072 tokens (88,448 cached
  input), exact `alpha.py`/`beta.py` scopes, three controller validations.

Both reports have zero failed/skipped/false-pass/human-intervention counts and
contain no business data.

### Residual boundary

Qualification remains procedural accountability: it proves who bound one
acceptance revision to one exact target definition and which gate reviewed that
mapping. It does not understand arbitrary business semantics and is not
cryptographic or external provenance. The medium-risk and closed state/schema
checkpoint below is now complete; execution provenance remains open and is not
relabelled as a P1 pass.

## P1 Medium Risk And Closed State/Schema Checkpoint

The P1 risk/state/schema candidate remains uncommitted and local. No commit,
push, merge, tag, release, deploy, user-install replacement, or business-project
migration occurred. The content identity immediately before appending this
self-referential audit section was:

```text
8df1587a342b68dcfd03e010d5c10b02c19895e971fb148aa3690cb2e8d00174
```

### Red evidence

- The initial medium-policy contract run had eight failing methods, including
  three table-driven acceptance cases. It proved that uncovered medium failure
  modes, open medium findings, incomplete/expired/stale acceptance, and empty
  degraded residual-risk notes were not consistently fail-closed.
- The initial closed state/schema run produced ten assertion failures and two
  explicit missing-contract errors across eight methods: free-form requirement
  status reached SQLite, canonical authorities were absent, all 18 schemas
  lacked versioned IDs, unknown-property policies were inconsistent, and the
  thin validator ignored supported constraints or accepted unsupported
  keywords.
- Four later adversarial projection tests were first run against production and
  produced three failures plus one error. They reproduced schema-30 candidate
  coupling, artifact and candidate TOCTOU false passes, and projection rebuild
  accepting an invalid canonical status. These are recorded as red failures,
  not as successful tests.

### Green evidence

- Medium/trust policy: 67/67 passed in 19.157 seconds with
  `ResourceWarning` treated as an error. Identified medium failure modes now
  require qualified structured current-candidate coverage or complete current
  accepted/exempt metadata; open medium findings block; degraded passing gates
  require explicit residual-risk text; high/critical behavior remains at least
  as strict.
- State/schema plus migration: 35/35 passed in 10.433 seconds with warnings as
  errors. The tests execute all shipped enum, minimum, const, minLength, and
  pattern constraints; enforce unique IDs and closed properties across all 18
  schemas; reject unsupported keywords; and prove CLI/API/DDL/doctor/migration/
  invariant/projection consumers share the canonical state authority.
- Migration tests preserve every legal requirement, acceptance, and
  failure-mode state and associated revision/acceptance metadata. Empty,
  whitespace-padded, and case-variant states are rejected without coercion.
  Schema-30 failure-mode `active` is the only normalization, and dry-run and
  real-manifest counts agree for zero, one, and multiple rows. Schemas 27, 28,
  and 29 also reject invalid entity states without publishing staging state.
- Projection verification now holds the project operation lock, backs up the
  database, snapshots referenced execution artifacts into a separate verifier
  root through `ProjectFS`, binds a frozen candidate, and rechecks artifact
  digests and candidate identity before returning. Schema 30 retains its
  candidate-independent historical renderer. Unsafe/escaping artifact paths
  cannot read or overwrite external authority.
- The complete P1 targeted command covered medium policy, trust, schema-30 and
  schema-31 contracts, doctor, migration, projection, lifecycle, runtime,
  operating-system, and feature-freeze suites: 147/147 passed in 59.632
  seconds with zero failures, errors, skips, expected failures, or unexpected
  successes and `ResourceWarning` treated as an error.

### Decisions and unresolved work

- The locked spec does not require a new public acceptance cancellation/mutate
  command, so no additional CLI surface was introduced. Acceptance state is
  still closed by guard, DDL, migration, doctor, projection, and JSON schema.
- No Critical, High, or Medium finding remains open for P1 medium-risk or
  state/schema scope after the deterministic TOCTOU and projection-state fixes.
- A documentation-contract probe after the P1 source edits was deliberately
  not green: 20/21 assertions passed and the current Native report assertion
  failed because its persisted `workspace_sha256` still describes the P0
  executable source. Task 10.11/10.12 owns marking that evidence historical and
  generating current single/parallel reports on the stabilized candidate; this
  failure is preserved as unresolved evidence and is not called a pass.
- Group 8 execution provenance, the remaining P1 manual-workflow closure, and
  all later governance/adoption work remain explicitly not run at this
  checkpoint. They are not implied by the results above.

## P1 Execution Provenance And Manual Workflow Checkpoint

The group-8 candidate remains uncommitted and local. No commit, push, merge,
tag, release, deploy, user-install replacement, or business-project migration
occurred. The content identity immediately before this audit update was:

```text
workspace_sha256 2643796e28c079c54c06ac16ae99fcd3ea7ddeac62e086bfe64193ed590f0a10
status_sha256    85b9153d6dda57cdf066088c0457cc3889737c28cae220e1d04a5f6c27f0fdef
git_head         e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb
git_dirty        true (intentional uncommitted implementation)
```

### Red evidence

- The initial provenance contract had ten expected assertion failures and four
  missing-contract errors across seven methods: execution rows lacked the
  target/controller/container fields, container runs did not resolve an
  immutable local image, and runtime/image/engine drift did not block commit.
- Two additional red assertions proved that the immutable execution projection
  omitted the new provenance facts and doctor did not identify a tampered
  `complete` row with an empty runtime fact.
- The documentation red contract proved that the retained guidance did not
  describe the schema-31 provenance fields, and the installed Skill proxy help
  did not state the already-local image boundary.
- The first broad targeted run was not a pass: 85 of 86 tests passed and one
  corruption-injection subtest errored because the new SQLite CHECK rejected
  the test's direct mutation before the delivery evaluator could inspect it.
  The fixture now explicitly bypasses CHECK constraints only for that tamper
  scenario; the production constraint was not weakened.

### Green evidence

- Every new schema-31 execution records `target_definition_sha256`, controller
  platform/runtime executable/version/digest, policy version, optional
  container engine/version/requested image/immutable digest, and
  `provenance_status=complete`. DDL, JSON schema, runtime validation, doctor,
  delivery, projections, and migration agree on the contract.
- Docker/Podman container verification requires an already-local image, runs
  its resolved immutable identity with `--pull=never`, and rechecks engine,
  engine version, requested image, and image digest before commit. Missing
  image or drift creates no passing execution/validation facts.
- A real local Docker capability run using the already-present
  `python:3.12-slim` image passed 1/1 in 1.004 seconds. It recorded a container
  execution with no-network/sandbox available and complete provenance. Podman
  was unavailable and was not called passing evidence.
- The corrected execution/structured/container/manual-journey targeted command
  passed 86/86 in 43.259 seconds with `ResourceWarning` as error and zero
  failures, errors, skips, expected failures, or unexpected successes. Together
  with the separate real-container check, the checkpoint contains 87 passing
  tests.
- Seven focused documentation, Skill-size, retained-proxy help, and local-only
  public-journey contracts passed; Plugin structure validation also passed.
  The public non-quickstart subprocess journey reached schema-31 delivery and
  persisted 64-hex target/runtime digests plus `provenance_status=complete`.
- The legal guidance now accepts the task before recording its revision-bound
  gate, binds that gate to the qualification, enters `delivery ready`, records
  delivery, and then validates it. It does not expose a generic phase mutator,
  Host lifecycle command, Connector runtime, or network trust shortcut.

### Remaining evidence boundary

Schema-30 and older executions migrate as `legacy-incomplete` history. They
remain inspectable but cannot satisfy a current schema-31 delivery. Local
environment provenance improves reproducibility; it is not independent identity
or cryptographic trust, so the high/critical `human-review-required` boundary is
unchanged. The persisted Native reports are still intentionally stale after
source edits and remain assigned to tasks 10.11-10.13; they are not counted as
current group-8 evidence.

## P1 Exit Performance Checkpoint

The five-sample schema-31 checkpoint was run with:

```bash
python3 -B benchmarks/run_local_core_benchmark.py \
  --facts 5000 \
  --samples 5 \
  --out /tmp/kafa-delivery-integrity-p1-exit.json
```

The benchmark deliberately records the full test suite as `not-run`; the
separate P1 union was 191/191 in 85.023 seconds, but injecting that targeted
duration into the report's `full_test` field would falsely label it as complete
discovery.

| Metric | Pre-change | Schema-31 checkpoint | Status |
| --- | ---: | ---: | --- |
| Fresh init median | 0.159367 s | 0.203088 s | comparative; no numeric startup/init gate is declared |
| One mutation after 5,000 facts | 0.017853 s | 0.023700 s | passes the <=0.050 s gate |
| Targeted three-view projection | 0.013683 s | 0.019105 s | comparative |
| Full 13-view projection | 0.067490 s | 0.089494 s | comparative |
| Fresh DB | 315,392 B | 380,928 B | 53,248 B above the previous 320 KiB budget |
| Plugin payload, caches excluded | 1,044,089 B | 1,229,427 B | 180,851 B above the previous 1 MiB budget |

The two size overruns are explicit justified deviations, not passes against the
old budgets. Schema 31 adds the qualification and outcome authorities, closed
state/schema enforcement, migration/rollback support, and execution provenance
needed to eliminate the reproduced false-delivery paths. The database remains
30 local-only product tables, and the Plugin adds no Connector, network
runtime, Host worker, or second lifecycle. Removing these authorities or
weakening their validators to recover the old size limits would reduce delivery
integrity; the final before/after audit will retain both deviations and report
artifact size again after source stabilization.

## P1 Adversarial Exit Review

The P1 executable-source snapshot after the final review fixes was:

```text
workspace_sha256 5f1f705e70ae4798e9e64e4a8e72ad57bdabeb1368d2704da0b98846122f7d39
status_sha256    31863ebfdd3ece19e2559c25f262a7c5f8127128b1ae601ea794cda2dc201ab3
git_head         e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb
git_dirty        true (intentional uncommitted implementation)
```

### Additional red evidence

The first independent execution review and the main-model follow-up found and
reproduced the following false-pass or contract-split paths before fixing them:

- local or container structured stdout could be truncated after an early
  positive suite, hiding a later failure;
- Go and nextest terminal events could appear before their starts, and nextest
  stress output with multiple complete suites conflicted with the original
  single-suite design text;
- an image ENTRYPOINT or container-engine stdout could bypass the intended
  controlled command/artifact path;
- a target with no declared `result_path` could directly write
  `/artifacts/structured-result` and override a failing stdout report, creating
  one passing execution, validation, and link;
- failure of `cp -a /src/. /workspace/` did not stop the target command, so an
  incomplete candidate copy could still report success;
- runtime, DDL, and JSON schema disagreed about engine/endpoint correlation,
  Windows npipe escaping and filename casing, relative engine names, empty Unix
  endpoints, and whitespace-only complete-provenance fields; and
- the public execution schema did not fully mirror runtime/DDL status and
  conditional complete-provenance constraints.

The original eight focused adversarial methods produced 23 assertion failures
and one error across their table-driven cases. Later red additions separately
produced one engine-alias failure, two container-wrapper/artifact failures, and
nine three-layer closure failures. These were deliberate red results, not
passes. The official nextest documentation confirms that libtest JSON is an
experimental versioned `0.1` format and that stress mode emits multiple
sequential complete suites. Docker's command reference confirms that
`--entrypoint` overrides the image entrypoint and `--pull=never` forbids an
implicit image pull.

### Green implementation and review

- Go and nextest are now order-aware state machines. Every started test must
  terminate, every relevant package/suite terminal must reconcile counts, and
  each nextest stress suite is checked independently. Over-limit structured
  stdout fails closed before facts are written.
- Container execution pins `--entrypoint /bin/sh`, runs setup under `set -eu`,
  captures target stdout only through the controlled artifact, rejects an
  undeclared structured artifact, clears and republishes a declared result only
  after the command, and never substitutes engine CLI stdout.
- Runtime, SQLite DDL, and the closed JSON-schema subset now agree on exact
  status domains, complete provenance, Docker/Podman path basenames, local
  endpoint types, mixed-case Windows executable names, non-empty endpoints, and
  non-whitespace facts. Schema-30 history remains `legacy-incomplete`.
- The affected combined command ran 101 tests in 42.681 seconds: 100 passed,
  one real-Docker capability test skipped, zero failed. The complete P1 union
  ran 282 tests in 108.939 seconds: 281 passed, the same capability test
  skipped, zero failed. Neither skip is counted as a pass.
- The final independent bounded re-review ran 58 tests: 57 passed, the same
  real-Docker capability check skipped because the sandbox could not access the
  local socket, and zero failed. It reproduced closure of both container High
  findings and all three-layer Medium findings and reported no remaining
  Critical, High, or Medium issue in its stated scope.
- Plugin structure, all repository JSON parsing, OpenSpec validation, and
  `git diff --check` passed. The documentation contract remains deliberately
  21/22: only the persisted Native report source identity is stale. Tasks
  10.11-10.13 own replacement on the final stabilized candidate.

The current-code real Docker capability rerun is pending explicit socket access
and is therefore not yet called passing. Real Podman, real Windows execution,
and an installed cargo-nextest binary are also not run at this checkpoint.

## P2 Outcome Metric Contract Checkpoint

Task 10.3 added one fixed local field-report contract:

```text
report_version   kafa-outcome-v1
metrics_version  kafa-outcome-metrics-v1
metric_version   kafa-outcome-metric-v1
evidence_mode    field
metric_count     6
```

Every metric now declares its event definition, unit, status/value, numerator,
denominator and applicability, observation window, `insufficient-data`
semantics, not-applicable condition, and reason. Explicit zero observations are
distinguished from missing facts. Historical-migrated deliveries are excluded;
qualification coverage reuses current acceptance revision and live target
digest; invalid, reversed, or future time windows fail as insufficient data.
The report takes one read snapshot and captures `generated_at` while the same
project operation lock remains held.

The initial five metric-contract tests all failed because the report had no
`metrics_version` or `metrics` surface. Two later adversarial red tests failed
because pre-window observations and historical deliveries were counted and
because outcome facts had no immutable triggers. After implementation, the
outcome/schema/migration checkpoint ran 53 tests in 16.711 seconds: 53 passed,
zero failed, zero skipped, and zero expected failures. OpenSpec validation,
Plugin structure validation, and `git diff --check` also passed.

The recovery rate currently aggregates persisted rows in the canonical
`migrations` table. A rollback represented only by a retained recovery manifest
does not silently become a field observation or success claim; absent persisted
attempt facts therefore remains `insufficient-data`. Task 10.4 separately owns
the deterministic four-scenario regression benchmark, which must remain
`regression-benchmark` evidence and must not alter these field metrics.

### Fixed P0 before/after outcome benchmark

`benchmarks/run_delivery_integrity_outcome_benchmark.py` now owns the immutable
`kafa-p0-false-delivery-v1` inventory: empty minimum graph, cancelled sole task
coverage, unrelated unqualified target, and direct record before readiness. The
before side points to the historical reproduced red checkpoint; the after side
runs the exact current regression method for each scenario with
`ResourceWarning` as error.

The current persisted report is
`docs/runtime/delivery-integrity-outcome-benchmark.json`. It records 4 historical
false-delivery results and 4 current fail-closed results, zero failed and zero
not-run after scenarios, identical before/after inventory, and a regression
closure rate of 1.0. Its evidence mode is `regression-benchmark`, not field.
All six field metrics remain explicitly `not-run` with null values because the
source repository is not an operator project and no completed field window was
observed. The benchmark writes no `outcome_observations` and claims no field
improvement.

The benchmark/report contract plus field metrics ran 22 tests in 9.799 seconds:
22 passed, zero failed, zero skipped, and zero expected failures. The generator
was also run directly and returned `status=passed`; OpenSpec validation and
`git diff --check` passed afterward.

### MIT license alignment

The root now contains the complete MIT text with
`Copyright (c) 2026 zygs1083-dotcom`; README links that file and `pyproject.toml`
retains the matching `MIT` expression and author. The source contract first
failed solely because the root file was absent, then passed after the addition.

An isolated source copy produced a real wheel and standard setuptools sdist.
Both contain the same complete LICENSE and both package metadata records contain
`Author: zygs1083-dotcom`, `License-Expression: MIT`, and
`License-File: LICENSE`. The artifact-backed license suite ran 3/3 with zero
skip or failure. This temporary checkpoint is not release evidence and did not
publish anything:

```text
wheel sha256 c63c88086d974272385a5b9376769a81cd4d639c95c2c667696bc0d379677025
sdist sha256 8b004ff8a401e848880d99aad7e083345e03822e864af74ffa6aaf2a042c4544
```

Task 10.9 will rebuild the final candidate artifacts and repeat these checks as
part of the no-publish release rehearsal.

### Build-only supply-chain pins

Primary documentation and immutable release metadata were rechecked on
2026-07-21. Search-index output initially exposed Syft 1.44.0, but the official
release API identified the newer immutable v1.48.0 release; the repository
therefore pins the authoritative current release rather than the stale search
result. `release-tooling.json` records Syft 1.48.0 source commit
`3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6`, CycloneDX JSON 1.6, and the
official SHA-256 for six supported macOS/Linux/Windows archives. GitHub build
and SBOM attestation is pinned to `actions/attest` v4.2.0 commit
`f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6`; deprecated wrapper actions are
not selected.

The machine-readable pin contract ran 2/2 tests with zero failure, skip, or
expected failure. JSON validation, OpenSpec validation, and `git diff --check`
also passed. The local statement assurance label is deliberately
`unsigned-local-integrity-statement`: exact byte binding is not represented as
an independent signature. No release workflow, tag, upload, or attestation was
triggered by this documentation/pinning step.

The first checkpoint attempted the repository's pre-existing `build==1.5.1`
CI pin and PyPI explicitly reported that version as yanked for breaking changes.
The pin contract was corrected before release tooling implementation to the
current non-yanked `build==1.5.0`; the workflow is updated in task 10.10. The
yanked install is not counted as successful release evidence.

### Standard SBOM and local provenance generation

`kafa.supply_chain` now accepts only one exact `kafa-2.0.0b1` wheel and one
standard sdist in a dist directory outside the source repository. It verifies
the pinned Syft version and source commit, emits one CycloneDX JSON 1.6 SBOM per
artifact, writes LF-only `SHA256SUMS`, and creates an in-toto Statement v1 with
the SLSA provenance v1 predicate. The statement binds both artifact SHA-256
values, current commit/status/content identity, the exact builder command,
build/Syft/Python facts, and the SBOM byproduct digests. A manifest written last
indexes every evidence-file digest. Local assurance remains explicitly
unsigned.

The generation contract was red before the module existed, then ran 5/5 tests
with zero failure, skip, or expected failure after implementation and pin
correction. A real pinned Syft 1.48.0 binary was downloaded to a temporary
directory, matched official archive SHA-256
`fef3e6d5df336a0a4c3e421e503119d1e221cf82a3ef5e426a791fcd81667e87`,
and reported source commit `3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6`.
It generated and verified two real artifact SBOMs in an ephemeral checkpoint.
That checkpoint is not the final candidate rehearsal: task 10.9 rebuilds the
then-current source and repeats installation plus post-install verification.

### Supply-chain tamper verification

Verification recomputes the exact two-artifact set from bytes and requires it
to equal the LF-only checksum subjects, both CycloneDX metadata subjects, the
two in-toto provenance subjects, and the supply-chain manifest. It also
recomputes source commit/status/content identity, tooling-manifest digest, each
SBOM byproduct digest, builder command/pins, and every evidence-file digest.
JSON duplicate keys, duplicate or extra subjects, artifact symlinks, and
generation-time source/artifact changes fail closed. Verification never
rewrites failed evidence.

The adversarial matrix covered artifact-byte mutation, checksum digest and
CRLF mutation, SBOM subject substitution, provenance subject and builder
substitution, source/tooling mutation, duplicate JSON keys, duplicate/extra
subjects, simulated symlink input, artifact/SBOM TOCTOU, source/SBOM TOCTOU,
and generated build-directory filtering. The full supply-chain contract ran
10/10 tests in 3.903 seconds with zero failure, skip, or expected failure;
compile, JSON, OpenSpec, and whitespace checks also passed.

### No-publish release rehearsal

`kafa.rehearsal` now snapshots the exact candidate without `.git`, caches, build
output, or egg metadata; verifies that snapshot against the current content
identity; builds exactly one wheel and one standard sdist with pinned
`build==1.5.0` and `setuptools==83.0.0`; generates and verifies supply-chain
evidence; runs the existing artifact-mode isolated install smoke; and verifies
the same artifact bytes again afterward. Its execution allowlist contains only
the pinned Python build command and the isolated smoke script. The smoke now
rejects renamed/symlink artifact inputs and returns both artifact SHA-256
values.

The first real invocation supplied a nonexistent `/opt/homebrew/bin/kafa` user
probe and failed before build/install; it is recorded as failed, not passed.
The corrected invocation used the verified `~/.local/bin/kafa` path and passed:

```text
source commit       e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb
source status sha   9eb765df4b32c9b76315305bf8d71afb5edf531a26b8fba7138c2ec30e1fc821
source tree sha     89237aae60e4cc26ea3009a0ad8f38c4cbb1fb210cd8ab1a9aa8eda2c9631d12
wheel sha256        b486fc400f553944a2d4e617ae954ed63ef6fc2f5d5ae227072e7b94b1ebe775
sdist sha256        4076a5bba04667be93df9b181b7a07a834d77d5fa3a958ab49c70107c48fa76a
wheel SBOM sha256   68691dc9e187098472d8f42c2b83d70701bb83c9ab1b4c85dee56c9ac6f29550
sdist SBOM sha256   5f0e4cf146d5061dd057cbe49c2fd3231c9e50baf96decbee3f641edab817236
```

The isolated smoke proved wheel import, marketplace/plugin discovery, cache
identity, 7 Skills, 3 Hooks, 3 templates, quickstart execution, schema-31
status, doctor, hook execution, and uninstall. Source identity, tag refs,
artifact bytes, and the observed user installation remained unchanged before
and after. The observed user state was Kafa/plugin `2.0.0-beta.1`; this task did
not install or replace it. No tag, release, upload, or deployment occurred.
The dedicated supply-chain/rehearsal suite ran 14/14 with zero failure, skip, or
expected failure. A broader 17-test command had 15 passes and two explicit
artifact-environment skips from `test_license_contract`; those skips are not
counted as passes and task 11 will run that contract with real artifact inputs.

### Attested release-candidate workflow

The tag-only release workflow now separates an immutable `candidate` job from
the authorized `publish` job. The candidate cannot start until the complete
three-platform verification matrix and the non-optional real Native Host
profile succeed. It installs the pinned build frontend/backend, resolves the
checksum-pinned Syft asset in one step and downloads it in the next, builds one
standard wheel plus one sdist exactly once, generates and verifies the local
SBOM/provenance bundle, runs the exact artifacts through isolated installation,
and verifies the same bytes again before attestation. The split Syft steps are
intentional: values appended to `GITHUB_ENV` are only available to subsequent
steps.

The official `actions/attest` action is pinned to commit
`f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6`. It creates build provenance for
both checksum subjects and one CycloneDX attestation per artifact. The publish
job downloads that exact candidate, never rebuilds it, revalidates local
evidence, verifies both SLSA and CycloneDX GitHub attestations against the exact
repository, workflow, source ref, and commit, and only then exposes the
pre-existing `gh release create` boundary. Candidate permissions are read-only
for repository contents; only the final publish job has `contents: write`.

The workflow contract and supply-chain/rehearsal tests ran 29/29 in 6.100
seconds with zero failure, skip, or expected failure. Ruby parsed both workflow
YAML files, `release-tooling.json` parsed successfully, and `git diff --check`
passed. No workflow, tag, attestation, upload, release, or deployment was
triggered; this is static and local workflow evidence only.

### Historical Native evidence boundary

Before running replacement profiles, the two report generations already in
repository history or the worktree are explicitly classified as historical
capability evidence. They cannot satisfy the current-candidate Native check:

| Generation | Profiles | Immutable identity | Classification |
| --- | --- | --- | --- |
| committed 2026-07-18 reports | single and parallel | report blobs `13559c0bc3e0f112a46e05bb97ca9ef822595b67` / `55f0c8f28ed0f6a9f65cbfdb477cd5ccd84ae3c3`; report-byte SHA-256 `2cd499f5fe59a05a50a2c6df0230ae6ad8a2a6b757ef3cc0388d385154e36083` / `acb3efd59e987a45606136ab4b1d165d95508bff2c2d6a2901c0245808822465` | historical; source HEAD `c99bf9bc0648079aa6823c0356599db9d58c84a1`, dirty status |
| P0 checkpoint 2026-07-20 reports currently at the canonical paths | single and parallel | report-byte SHA-256 `a1a89cdea9a542ccfa481a80cdc99de53fa819413e8c564f2ba9500cb877de13` / `a2f8804700f016b100de7c4ddb154989217ac8cefd1a7398b697de7c0ef4512d` | historical; workspace SHA-256 `2719f07036be10e7a7931b83bc9cd2110520eb37201941593588dcce9fc941e2`, status SHA-256 `eae79170b8e04a2b6173060376b357b849fa2df6850b1c5ea9635139bb867f1f` |

The existing JSON remains intact until the replacement command completes, so a
failed or blocked Native invocation cannot erase the last truthful historical
record. Only a passing report that survives the evaluator's current source,
status, binary, token, scope, and timing checks may replace the canonical file.

### Current Native Codex single and parallel evidence

The first two single-profile invocations were real failures, not passes. The
sandbox could not resolve or connect to `chatgpt.com`; each Native process
returned 1 after transport retries, changed no file, emitted no completed token
usage, and never reached controller verification. The second invocation wrote
debug output only under `/private/tmp` and confirmed DNS/network restriction as
the cause. They are counted as two failed attempts and provide no compatibility
evidence.

That checkpoint exposed a separate persistence defect: `--evidence-out`
rejected an inconsistent report but still overwrote prior evidence when a
report was structurally consistent and operationally failed. The new red test
proved the overwrite. The writer now evaluates `should_fail()` before any
persistent write, leaves an existing evidence file byte-identical on failure,
emits `refusing failed persistent evidence`, and still permits an explicit
diagnostic `--out`. The focused persistence regression ran 2/2 with zero
failure, skip, or expected failure after the fix.

With the user-authorized synthetic prompts and temporary test files, the
single and internal two-producer parallel profiles were then run with network
access and passed. Both reports bind the same exact executable candidate:

```text
git head              e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb
git dirty             true (exact dirty identity is recorded, not called clean Git)
status sha256         34ac8d032cad4a1f6a3d5481a1033f02ec62e4b0f60820acc4a0b0b69e9c127a
workspace sha256      0e9321ae454f0c7aaa3e2873ca198b870653af54f9894f3d1a8004373b5dfd21
Native binary sha256  d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406
```

| Profile | Result | Tokens | Native runtime | Scope/timing evidence | Report SHA-256 |
| --- | --- | ---: | ---: | --- | --- |
| single | 1/1 passed; 0 failed/skipped/false-pass/human-intervention | 51,980 | 29.877104s | only `candidate.py`; controller verify passed | `7006cb169c9c2843ddca190e068dd4698fbca9d11801c18e28c23a20ad9c4a1b` |
| parallel | 1/1 passed; 0 failed/skipped/false-pass/human-intervention | 117,427 | 44.458496s | two isolated producers changed only `alpha.py` / `beta.py`; overlap 39.831881s; both targeted plus combined verification passed | `e0fedb9a0aa749ac5861119205f4fb3587009e2d32e8c59d505a2b330893c3bf` |

Strict persisted-report validation required the current binary, Git state, and
runtime matrix and returned an empty error list for both files. The dedicated
documentation/persistence checks ran 2/2; both JSON files parsed and
`git diff --check` passed. These are current local capability and workflow
evidence, not delivery provenance, an independent product review, a release,
or a deployment.

### Main branch governance

Authenticated read-only GitHub API calls established the baseline before the
write: `main` returned HTTP 404 `Branch not protected`, repository rulesets
were `[]`, the repository was public, and the current identity had admin
permission. The successful checks on `e3d46d9...` supplied the exact supported
contexts below; every one came from GitHub Actions App ID 15368:

- `harness (ubuntu-latest, python3, 3.11, stability)`
- `harness (macos-latest, python3, 3.12, fixture)`
- `harness (windows-latest, python, 3.11, fixture)`

The update route and `--input` semantics were verified through `gh api --help`,
and the request body was checked as JSON before use. Its SHA-256 was
`a635a15ada1588cf1239a21bf41492053811651ac812148f6fe7f114ad47a42f`.
The protected-branch REST contract was cross-checked against official GitHub
documentation. One reversible PUT then enabled strict/up-to-date status checks
pinned to App ID 15368, one approving review, stale-review dismissal,
conversation resolution, administrator enforcement, and disabled force pushes
and deletions. It did not enable signatures, linear history, branch locking, or
fork syncing.

A separate GET returned the same complete protection object; the contexts
endpoint independently returned the same ordered three contexts, and the
ruleset list remained empty. The exact normalized response and the unexecuted
DELETE reversal endpoint are persisted in
`docs/runtime/github-main-protection-evidence.json`. Only the classic `main`
protection changed externally. No commit, push, tag, release, or deployment was
performed.

There is currently only one collaborator with push/admin permission. Because
administrators are intentionally covered and one independent approval is
required, merges are blocked until a second eligible reviewer is added or the
reversible rule is deliberately changed. This is an explicit governance effect,
not described as a transparent or no-impact setting.

### P2 exit checkpoint

The combined outcome, benchmark, release-rehearsal, supply-chain, workflow, and
documentation command ran 73/73 tests in 16.816 seconds. The complete Native
report/evaluator suite ran 47/47 in 103.756 seconds. Both commands used
`ResourceWarning` as an error and reported zero failures, errors, skips,
expected failures, or unexpected successes.

The first current-source install smoke was not successful: sandbox DNS blocked
PyPI build-dependency resolution, so it returned nonzero and is recorded as a
failed environment attempt. The identical command was rerun with approved
network access and passed wheel import, marketplace/plugin/cache discovery,
schema-31 quickstart/status/doctor, 7 Skills, 3 Hooks, 3 templates, direct hook,
and uninstall. It produced an ephemeral wheel SHA-256
`d6a202e0d38f49978d88a03c9aaeb196c6d20dbb51e73e9b316c3451c7f621ad`;
artifact-mode source archive and live Host hook execution were not part of this
particular smoke and are not claimed from it.

A final local assertion revalidated every persisted governance field and both
Native reports against the current executable source, binary, Git status, and
matrix. Five JSON artifacts parsed, OpenSpec status reported 4/4 artifacts,
OpenSpec validation passed, and `git diff --check` passed. KAFA-P2-1 through
KAFA-P2-3 are closed from this current evidence; the sandbox install failure
remains explicitly separate from the successful rerun.

### Complete targeted acceptance checkpoint

The first 639-test high-risk command produced 622 passes, 13 skips, three
failures, and one error. Two install failures were sandbox PyPI DNS failures;
one path-safety error was the sandbox denying a temporary Unix socket; and one
real regression showed the feature-freeze allowlist still expected 56 CLI
nodes after the locked outcome work added `cycle outcome-record` and
`cycle outcome-report`. The two commands were added to the explicit allowlist,
whose exact expected count is now 58. The four focused reruns passed 4/4 with
network/system capability enabled.

The complete 639-test command was then rerun with the required system/network
capabilities. It produced 626 passes, 12 platform skips, and one failure in
226.824 seconds. All 12 skips are the explicit Windows-only ProjectFS contracts
on this macOS host; they are not counted as passes. The sole failure was the
expected fail-closed Native report check: changing the feature-freeze test
changed the controlled executable-source digest. Tasks 10.12/10.13/10.16/10.17
were immediately reopened instead of representing the reports as current.

The independent single and two-producer profiles were safely parallelized
because they use separate temporary roots and output paths. Both replacement
reports passed against source status SHA-256
`34ac8d032cad4a1f6a3d5481a1033f02ec62e4b0f60820acc4a0b0b69e9c127a`
and workspace SHA-256
`53c0d1dd9d0032960657fd97232c7ac384850f88d9ac18e8a665675904b98cc8`:

- single: 1/1 passed, 51,799 tokens, 35.315778 seconds, report SHA-256
  `e89a6a619e115984bd5cbb266e5fbcfebb19c770b330851d58f42e026df2ae8b`;
- parallel: 1/1 passed, 104,831 tokens, 49.202602 seconds, overlap
  47.567945 seconds, report SHA-256
  `3b60e69bc67ab408b306b4fb27826e61fa5d45f5dcd24d00da3ab5c528d3561f`.

The feature-freeze plus current-report closure ran 2/2. The refreshed P2
outcome/release/docs gate ran 74/74 in 19.618 seconds; the current-source
isolated install passed and produced ephemeral wheel SHA-256
`4a751ccc2ace14fda061f919c262c33f3d5bc1ee76a6c7048c750948d5375206`.
Governance and both reports revalidated, OpenSpec validation and whitespace
passed, and P2 was reclosed from current evidence. Combining the unchanged
626 passing targeted cases, 12 explicit Windows-only skips, and the focused
current-report rerun closes task 11.1 with no unresolved failure or error; the
12 skips remain skips rather than passes.

### Complete unittest discovery

After the task-11.5 artifact acceptance was strengthened, the final
current-source complete discovery command ran 738 tests in 362.979 seconds
with `ResourceWarning` as an error: 724 passed, 0 failed, 0 errors, 14 skipped,
0 expected failures, and 0 unexpected successes. The skips are not included in
the 724 pass count. Twelve are explicit Windows-only ProjectFS contracts on
this macOS host. A verbose focused query confirmed the remaining two are the
wheel/sdist LICENSE assertions when `KAFA_TEST_WHEEL` and `KAFA_TEST_SDIST` are
not supplied; their artifact-backed execution is required separately by task
11.5 and is not inferred from discovery. Runtime smoke invoked within discovery
also reported 2/2 scenarios, and the local-core benchmark fixture wrote its
temporary report without error.

### Runtime and deterministic E2E profiles

Five independent local profiles were parallelized because they use disjoint
temporary project state and evidence paths. Runtime smoke passed 2/2 scenarios;
the Skill transcript satisfied all 22 required markers; fixture E2E passed 6/6
with zero failure, skip, false pass, SQLite lock error, or human intervention;
and stability E2E passed 11/11 under the same zero counts. Stability included
structured/no-network fail-closed behavior, cycle isolation, 12 concurrent
SQLite operations without lock/thread leakage, schema-27 to schema-31 success
plus injected rollback, and the exact installed 7-Skill/3-Hook/3-template
surface.

The fixed delivery-integrity outcome profile regenerated its persisted report
with `status=passed`: all four historical bypass scenarios remain reproduced
as the before side and all four current scenarios fail closed as the after
side. No field window was invented. Both current Native reports still matched
the executable source afterward.

### Structure, schema, and boundary validation

The final plugin structure validator passed. A duplicate-key-rejecting parser
loaded all 34 repository JSON files outside generated/cache directories. The schema,
feature-freeze, control-plane, local-core, kernel-module, and Native-ownership
contract set ran 59/59 in 10.887 seconds with no failure or skip, covering the
closed Draft 2020-12 IDs/keywords and the canonical local-only/Host-ownership
boundaries. OpenSpec status reported 4/4 artifacts, OpenSpec validation passed,
and `git diff --check` returned no error.

### Exact artifact installation and migration checkpoint

Task 11.5 first exposed three real evidence gaps in the isolated installation
profile: it initialized only schema 31, discarded the concrete source/managed/
cache tree digests after doctor, and stopped after Codex registration removal
without exercising the Kafa user-scope uninstall path. Two rehearsal contract
tests and one doctor-digest parser test reproduced those gaps before the
production smoke and rehearsal gate were tightened. The focused red tests
failed 2/2 plus one error as expected; the corrected release/rehearsal set then
ran 33 passes with two explicitly artifact-dependent skips. Those two LICENSE
checks subsequently passed 2/2 against the exact artifacts below, making the
artifact-backed LICENSE profile 3/3 including repository metadata.

The current candidate was built once with pinned `build==1.5.0` and
`setuptools==83.0.0`, then generated and verified with the pinned Syft 1.48.0
binary. The exact subjects are:

| Subject | SHA-256 | CycloneDX SHA-256 |
| --- | --- | --- |
| `kafa-2.0.0b1-py3-none-any.whl` | `55b335d112ef222a58e04c46051e2c23e543bb18fb84885d36858722454d3cf0` | `08a8fa660cd0ddfaddf284e19de143935c2eb002a65cbf615a810dfebd0db52f` |
| `kafa-2.0.0b1.tar.gz` | `ee9fb12a5749d63a981e3132e0ac7c8e73e2a2dfe179b4a1cfd1793dce8feec9` | `677258edd01ed7bf5edd243f971cec3f6a17f0163ee409fac58bb38552222cf1` |

The local supply-chain verifier passed both before and after installation. Its
source tree SHA-256 is
`196c94f64e4e2adf50cbc48e80e27200a4e37dcd4d40b5575e8de62e0c1284b0`;
the assurance remains truthfully labelled
`unsigned-local-integrity-statement`, not a published GitHub attestation.

The artifact-mode smoke installed the exact wheel into an isolated venv,
extracted the exact sdist, installed and registered the extracted Plugin in an
isolated HOME/CODEX_HOME, and passed real app-server discovery for 7 Skills,
3 Hooks, 3 agent templates, 18 schemas, and 7 runtime scripts. It completed the
fresh schema-31 quickstart and the direct installed-cache hook handler. It does
not claim that a live authenticated Host turn invoked the hook.

The same installed cache then created a minimal schema-30 business graph using
its own `core.schema_lifecycle` and `harness_db` modules; both loaded paths were
verified to be descendants of the installed cache rather than the checkout.
The dry-run kept the database byte-identical and created no backup, migration
sentinel, or production projection. The real migration reached schema 31,
preserved the cycle/requirement/acceptance/two links, normalized exactly one
legacy active failure mode to `identified`, invented zero qualification/gate-
qualification/outcome authority rows, passed FK checks plus both Kernel and
public project doctor, and generated the 13-view projection set. Its verified
schema-30 backup SHA-256 was
`8fffd2c1bb87f9d51e8ce99211dd775686be42338a91fcc50e2263771dec1f8d`;
the activated migration manifest SHA-256 was
`92c3293688ce84b40e65884a57b7a1a3437cce9dcad9639c4c3a4edfe2355c05`.
This migrated only an ephemeral test project, not a user business database.

Doctor published and the smoke parsed these exact equal tree digests:

```text
artifact source plugin  c7ecf0d0c4f7fb5d2601212aa105a7e95d224daad0894dc9a0fef26dba57de29
managed user plugin     c7ecf0d0c4f7fb5d2601212aa105a7e95d224daad0894dc9a0fef26dba57de29
Codex cache plugin      c7ecf0d0c4f7fb5d2601212aa105a7e95d224daad0894dc9a0fef26dba57de29
```

Finally, `codex plugin remove` removed the isolated registration and its cache;
the artifact-installed `kafa plugin uninstall --scope user --remove-files`
removed the Plugin entry from the retained marketplace document and removed
the managed copy. Post-uninstall installed and available lists contained no
Kafa Plugin. The isolated HOME was then discarded. No current user installation
was replaced.

### Final performance and size checkpoint

Task 11.6 ran three independent five-sample schema-31 benchmarks against one
unchanged executable-source identity (`status_sha256=34ac8d032cad4a1f6a3d5481a1033f02ec62e4b0f60820acc4a0b0b69e9c127a`,
`workspace_sha256=727b2add6af00f2409558531c12a8fe665caa9b54d39fb72d4574fd8d79da758`).
The report's payload key remains named `schema30` for compatibility, but every
run records actual schema 31 and Runtime 5.0.0; the key is not evidence of an
old runtime.

| Metric | Current result | Baseline/budget | Truthful status |
| --- | ---: | ---: | --- |
| 5k-fact mutation, three group medians | 34.921 / 26.420 / 27.802 ms | <=50 ms | passed in all three groups; worst headroom 15.079 ms |
| Fresh DB | 380,928 B | <=320 KiB | exceeded by 53,248 B |
| Plugin regular files, caches excluded | 1,289,387 B / 71 files | <=1 MiB | exceeded by 240,811 B |
| Fresh init, median of group medians | 206.299 ms | 159.367 ms baseline | comparative, 29.45% slower; no current numeric gate |
| Targeted three-view projection | 23.010 ms | 13.683 ms baseline | comparative, 68.17% slower; no numeric gate |
| Full 13-view projection | 95.428 ms | 67.490 ms baseline | comparative, 41.40% slower; no numeric gate |
| Cold CLI `--help` | 94.170 ms | none declared | measured, not a pass/fail gate |
| Cold initialized `status` | 122.030 ms | none declared | measured, not a pass/fail gate |
| Public 5k-fact 13-view rebuild | 208.247 ms | none declared | seven-sample median |
| Warm delivery evaluator | 28.852 ms | none declared | 21-sample median, zero blockers, delivery allowed |
| Cold public `validate --delivery` | 286.157 ms | none declared | 11-sample median on a deliverable graph |

The DB and Plugin size values remain explicit justified deviations, not passes
against the retired budgets. Their added schema-31 qualification/outcome
authorities, migration recovery, provenance, and supply-chain checks are not
removed merely to recover the old byte totals. The benchmark declares
`timing_assertions=false`; only the explicit 50 ms mutation budget is treated as
a timing gate here.

### Final before/after surface and outcome metrics

The frozen baseline's total LOC is correct, but its test-only subtotal was a
transcription error: recomputing the frozen HEAD blobs gives 24,149 test LOC,
not 23,774. The corrected subtotal reconciles to the already recorded 51,725
total and is used below without rewriting history.

| Surface | Before | After | Delta |
| --- | ---: | ---: | ---: |
| Python files | 66 | 81 | +15 (+22.727%) |
| Total Python physical LOC | 51,725 | 68,829 | +17,104 (+33.067%) |
| Plugin Python physical LOC | 25,503 | 31,833 | +6,330 (+24.821%) |
| Test Python physical LOC | 24,149 corrected | 33,065 | +8,916 (+36.921%) |
| Active product tables | 27 | 30 | +3 |
| Public JSON schemas | 16 | 18 | +2 |
| Recursive CLI parser nodes | 53 | 58 | +5 |

The three tables are the locked qualification, gate-qualification, and outcome
authorities. The two schemas are qualification and outcome-observation. The
five CLI nodes are `baseline confirm`, `delivery ready`, `test-target qualify`,
`cycle outcome-record`, and `cycle outcome-report`. No Connector, Host worker,
or second task lifecycle is included. The LOC increase is disclosed as an
implementation/audit cost, not described as slimming or as a quality result.

The last verified pre-change artifact snapshot was 30,030 B wheel plus
370,134 B sdist. The task-11.5 build-time snapshot is 46,476 B wheel plus
483,449 B sdist: +54.765%, +30.615%, and +32.427% combined. Its additional
SBOM/checksum/provenance/manifest sidecars are 11,731 B. Because the checklist
and this audit were updated after the build, a verifier pointed at the mutable
live worktree now correctly reports `supply-chain source identity mismatch`.
The artifact remains a valid immutable build-time snapshot of source tree
`196c94f6...`; it is not called the final live-worktree artifact. The final
no-publish rehearsal uses a separate immutable source snapshot so evidence
updates cannot create a self-referential rebuild loop.

Current Native reports now share exact status SHA-256
`34ac8d032cad4a1f6a3d5481a1033f02ec62e4b0f60820acc4a0b0b69e9c127a`,
workspace SHA-256
`727b2add6af00f2409558531c12a8fe665caa9b54d39fb72d4574fd8d79da758`,
and binary SHA-256
`d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`:

| Native profile | Before | Current | Change |
| --- | ---: | ---: | ---: |
| Single producer tokens | 51,892 | 51,906 | +14 (+0.027%) |
| Single producer runtime | 53.657465 s | 60.780545 s | +7.123080 s (+13.275%) |
| Parallel producer tokens | 115,682 | 103,983 | -11,699 (-10.113%) |
| Parallel runtime | 81.432381 s | 57.479989 s | -23.952392 s (-29.414%) |
| Parallel producer overlap | 77.067911 s | 48.781920 s | -28.285991 s (-36.703%) |

The current compact report SHA-256 values are
`452680fcdc5f4032eee3ceaea944d7a6a2bc44d578ce0767173ca82d0b3b49ee`
for single and
`97adb7bf5b9da9de56e2fb9aba504036b6a565a129b1370758818f998dc2fe8f`
for parallel. They prove local Native capability, scope, and timing only; token
or latency movement is not treated as a product-quality guarantee.

Before hardening there was no canonical qualification authority, and the fixed
unrelated target could falsely satisfy acceptance. The current fixed benchmark
reproduces all four historical false-delivery cases and closes all four current
cases fail-closed, for regression closure 4/4 and 1.0. That report is bound to
the same current executable workspace and has SHA-256
`50fb0d6a3af99acd2e433a92afbd61c20b18729432c002c3bb7d56bad55139ac`.
Its field-mode `qualification_coverage_rate` and
`false_green_prevented_count` remain `not-run`/null, and
`field_improvement_claimed=false`, because no operator project or complete
field window was observed.

### Final local-only and external-effect boundary

The structure validator and 30/30 focused local-only, Native Host-ownership,
and feature-freeze contracts passed. The active schema adds only the three
qualification/outcome tables named above. Production runtime contains no new
GitHub, Linear, Notion, Figma, or Slack SDK, Connector token, business `gh api`,
Host SDK worker, agent/session/dispatch/lease/worktree authority, fabricated
external receipt, or second task lifecycle. Legacy Connector/agent/receipt
names remain confined to isolated historical migration, where their trust is
downgraded; Native evaluator workers are test-only concurrency, not Host
runtime ownership.

User state was re-read after the isolated install: `kafa --version` remains
`2.0.0-beta.1`, and `codex-project-harness@personal` remains version
`2.0.0-beta.1`, installed and enabled at the same managed path. The source repo
contains no `.ai-team/state/harness.db` or backup tree. Every migration test in
this change used a temporary project, so no business project database was
migrated.

Remote readback still shows no tag at current HEAD, no run of the release
workflow, no deployment, no repository Actions secret, and no new release; the
latest remote tag/release remain the old `v1.21.3-beta.1` prerelease lineage.
No commit or push occurred in this change. The only external repository setting
changed was the separately recorded task-10.14 `main` branch protection. No
tag, release, deployment, production migration, secret change, paid resource,
or user installation replacement occurred. Native E2E did consume the already
authorized model service and is not mislabelled as zero model usage.

### Final P2 closure and no-publish rehearsal

The final P2 targeted gate executed 128 outcome, release, installation,
LICENSE, workflow-static, documentation, feature-freeze, and Native-install
tests. Its first sandboxed run produced 126 passes and two failures solely
because isolated source installs could not resolve PyPI build dependencies.
The exact two tests were rerun with approved network access and passed 2/2 in
6.569 seconds. This is recorded as a recovered environment failure, not as a
single clean 128-test run. The separate Native report/evaluator suite passed
47/47 in 105.898 seconds with no failure or skip.

The pinned no-publish rehearsal used `build==1.5.0`, `setuptools==83.0.0`,
Syft 1.48.0, and Codex CLI 0.143.0 against an immutable snapshot of source tree
`6e11e85caa7b5c552181625adffa7a8e5c4f7a95c6b5263be0b2301a11d902fe`.
It built two artifacts, generated two CycloneDX 1.6 SBOMs, verified the local
unsigned integrity statement before and after isolated installation, migrated
an installed schema-30 fixture to schema 31, passed discovery/quickstart/
doctor/direct-hook checks, and removed both the isolated Codex registration and
managed Kafa plugin. Source, tag refs, artifact bytes, and the observed user
installation were unchanged. The retained external report is
`/private/tmp/kafa-delivery-integrity-final-rehearsal.json`, SHA-256
`bbbb0ee3b94d1e339df57c1ca6ac9560c44f8b3c833d2670b908af306fcee6d2`.

A fresh GitHub API readback confirmed `main` still requires a pull request,
one approving review, resolved conversations, strict Ubuntu/macOS/Windows
checks, and applies protection to administrators; force-push and deletion stay
disabled. The repository license API remains `null` because the new LICENSE is
not committed or pushed, and no remote GitHub attestation exists because no
push/release workflow was authorized. Those release-activation facts remain
`not-run`; they are not relabelled as local passes. KAFA-P2-1 through P2-3 are
closed for the exact local candidate with these explicit release gates retained.

### Independent-QA candidate checkpoint

The candidate handed to independent QA is branch `main` at
`HEAD=origin/main=e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb` with an intentional dirty
worktree and no commit. The executable-source identity helper reports 86 status
entries, status SHA-256
`34ac8d032cad4a1f6a3d5481a1033f02ec62e4b0f60820acc4a0b0b69e9c127a`,
workspace SHA-256
`727b2add6af00f2409558531c12a8fe665caa9b54d39fb72d4574fd8d79da758`,
and Codex binary SHA-256
`d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.
Audit/checklist/OpenSpec text is outside that executable-source scope; any later
runtime/schema/test edit invalidates this checkpoint and both Native reports.

The QA input includes these authoritative current results and commands:

- targeted acceptance with `-W error::ResourceWarning`: 627 passes, 12 explicit
  Windows-only skips, one expected stale-report fail-closed result, followed by
  the current-report focused closure; 628 unique non-skipped cases are green;
- complete `unittest discover`: 724 passes, 14 skips, zero failure/error out of
  738 tests in 362.979 seconds;
- runtime smoke 2/2, Skill transcript 22/22 markers, fixture 6/6, stability
  11/11, fixed outcome scenarios 4/4;
- schema/local-only structure set 59/59, 34 JSON files parsed with duplicate-key
  rejection, Plugin structure valid, OpenSpec 4/4 and valid, whitespace clean;
- Native single 1/1 and parallel 1/1 with the exact shared candidate identity;
- immutable artifact installation/migration/uninstall checkpoint and the final
  no-publish rehearsal report identified above;
- P2 combined gate: 126 initial passes plus two network-recovered exact reruns,
  and independent Native report/evaluator 47/47;
- fresh GitHub branch-protection readback, while clean commit/push, remote
  three-platform CI for this candidate, GitHub license detection, attestation,
  tag, release, and deploy remain explicitly `not-run`.

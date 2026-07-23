# Kafa Local Workflow Lightweighting Baseline

Date: 2026-07-22
Source: `main@7c7aa41929426bc1d89350497ceb2c9266290b88`
Change: `local-workflow-lightweighting`

## Boundary Baseline

- Runtime/kernel/schema: `5.0.0` / `5.0.0` / schema 31.
- Active product tables: 30.
- Public JSON schemas: 18.
- Public runtime CLI surface: 59 parser nodes.
- Distribution: 7 Skills, 3 Hooks, 3 Native agent templates.
- User installation, business-project databases, release state, tags, and remote
  branches were not changed while collecting this baseline.

## Workflow And Source Size

The seven currently independent workflow/guidance consumers occupy 69,620
UTF-8 bytes in total. This is a maintenance/context surface measurement, not a
claim that every byte is duplicated.

| File | Bytes |
| --- | ---: |
| `README.md` | 23,058 |
| `QUICKSTART.md` | 12,508 |
| `plugins/codex-project-harness/skills/project-harness/SKILL.md` | 12,711 |
| `plugins/codex-project-harness/docs/TRIGGER_MATRIX.md` | 1,592 |
| `examples/full-project-flow.md` | 11,254 |
| `docs/runtime/skill-eval-transcript-fixture.txt` | 2,943 |
| `docs/runtime/fresh-skill-eval-prompts.md` | 5,554 |

The five main implementation surfaces contain 10,166 lines:

| File | Lines |
| --- | ---: |
| `scripts/harness.py` | 783 |
| `scripts/harness_db.py` | 5,108 |
| `core/delivery.py` | 2,252 |
| `core/projections.py` | 861 |
| `kafa/cli.py` | 1,162 |

The complete manual setup shown in QUICKSTART requires the controller to keep
separate requirement, acceptance, failure-mode, baseline, task, target, link,
qualification, and task-state identifiers. `quickstart minimal --execute`
reduces commands but uses multiple fact transactions and also performs
verification, so it is not an atomic plan setup.

## Default Output Baseline

Measurements used a fresh temporary directory, source runtime, Python `-B`, and
no user or business-project state.

| State / command | Exit | Bytes | Lines |
| --- | ---: | ---: | ---: |
| uninitialized `status` | 1 | 588 | 3 |
| uninitialized `doctor` | 1 | 588 | 3 |
| uninitialized `quickstart status` | 0 | 614 | 7 |
| initialized `status` | 0 | 192 | 11 |
| initialized healthy `doctor` | 0 | 26 | 1 |
| initialized empty `quickstart status` | 0 | 3,392 | 23 |

The initialized quickstart default exposes seven delivery-issue lines and eight
scaffold commands. The P0 target for this noisy initialized-empty quickstart is
at most 25% of its 3,392-byte baseline and no more than one primary action.
Status and doctor instead require an exact three-line card and at most one
action: their 192-byte and 26-byte baselines make a universal 25% threshold
mathematically incompatible with the required three labels. Verbose/JSON retain
the full report.

## P0 Red-Test Baseline

These are intentional pre-production failures, not passing evidence:

| Contract suite | Tests | Pass | Fail assertions | Errors | Skip / expected-failure |
| --- | ---: | ---: | ---: | ---: | ---: |
| workflow contract | 8 | 8 | 0 | 0 | 0 |
| delivery-plan / verified-patch | 11 | 0 | 13 | 0 | 0 |
| concise operator output | 10 | 0 | 20 | 0 | 0 |
| derived delivery narrative | 8 | 1 | 4 | 3 | 0 |

The workflow suite became green after adding the presentation contract and
renderer. The other three rows preserve the red baseline before their
production surfaces are changed.

Existing positive behavior was then checked under
`-W error::ResourceWarning` without counting skipped tests as passes:

| Existing contract group | Pass | Fail / error | Skip |
| --- | ---: | ---: | ---: |
| delivery prerequisites and immutable execution | 76 | 0 | 0 |
| local delivery/trust policy | 42 | 0 | 0 |
| schema/migration/path safety/operation lock | 188 | 0 | 12 |
| single-writer task lifecycle and Store seam | 17 | 0 | 0 |

The 12 skipped cases remain skipped. An initially misspelled nonexistent test
module produced an invocation error; it was corrected only after `rg --files`
identified the actual module and is not counted as either a product failure or
a passing test.

## P0-1 Checkpoint

- Workflow contract/renderer: 8/8 passed.
- Architecture/control-plane plus installed-reference copy: 5/5 passed.
- Documentation, local-core, single-writer, feature-freeze, install-copy, and
  public-journey selection: 63 passed; the persistent Native-evidence
  consistency check correctly failed because its source digest is now stale.
- Skill eval: 23 contract-derived markers passed.
- Structure validation and workflow `--check`: passed.
- Guidance surface after P0-1: 51,857 bytes, down 17,763 bytes (25.51%).
- `project-harness/SKILL.md`: 11,904 bytes; Skill plus default delegation
  reference: 15,033 bytes.

The stale Native report is not relabelled as passed. It will be refreshed only
after the final selected source scope is stable, avoiding evidence churn after
each intermediate edit.

## P0-2 Checkpoint

- The closed version-1 delivery-plan parser rejects missing, extra, renamed,
  duplicate, and wrong-typed fields; CLI JSON errors remain one object with no
  implicit initialization.
- Plan application now shares the same connection-scoped requirement,
  acceptance, failure-mode, task, target, relation, qualification, event, and
  project-revision primitives as individual commands. One `BEGIN IMMEDIATE`
  creates the complete graph; exact replay writes and republishes nothing.
- A deterministic late-qualification injection proved full transaction
  rollback. A separate post-commit projection failure proved that the complete
  fact graph remains committed, projection verification reports it stale, a
  supported rebuild restores byte agreement, and replay remains a no-op.
- `verified-patch` reuses immutable `verify_run()` and returns the locked
  12-field read model. Candidate, acceptance revision, target digest, and
  qualification changes fail closed; task, phase, gate, delivery, and Host
  lifecycle remain untouched. A cancelled task stays cancelled and delivery
  remains blocked.
- Delivery-plan/verified-patch contract: 24/24 passed with
  `ResourceWarning` treated as error.
- Broader execution, qualification, lifecycle, delivery-cycle, Store,
  operation-lock, projection, path-safety, schema-30/31 migration, rollback,
  architecture, feature-freeze, and stop-ship selection: 376 tests ran;
  364 non-skipped tests passed and 12 remained skipped.
  The skips remain skips and are not counted as passes.
- Independent P0-2 review initially found three High and one Medium issue:
  post-verification candidate drift, trusted hand-built model bypass, missing
  complete-graph commit postcondition, and incomplete initialized dry-run
  preflight. Follow-up review also found a superseded-gate read-model leak and
  exact-replay dry-run noise. Main fixed all findings, added deterministic
  regressions, and obtained a bounded re-review with 0 Critical, 0 High, and 0
  Medium remaining. The independent reviewer ran 134 passing tests with no
  skips; its two internal axes (113 projection/migration tests and 24 spec-axis
  tests) overlap that evidence and are not added to the main totals.

Seven independent temporary schema-31 projects were seeded with 5,000 local
facts each. The first timing probe deliberately used a non-gateable generic
Python command and was rejected before mutation; it is an invalid benchmark
invocation, not a product pass. Re-running with a registered unittest command
produced:

| P0-2 metric | Samples (seconds) | Median | Budget / status |
| --- | --- | ---: | --- |
| Atomic plan apply plus affected projection publication | 0.040688, 0.039276, 0.040438, 0.039354, 0.038738, 0.038207, 0.039991 | 0.039354 s | <=0.050 s, passed |
| Exact semantic replay | 0.010266, 0.009439, 0.009618, 0.010138, 0.009899, 0.009757, 0.010089 | 0.009899 s | comparative, no writes |

The pre-optimization valid measurement was 0.068443 seconds median and failed
the 0.050-second budget. Reusing one verified SQLite read connection across a
multi-view projection publication, and replacing the redundant pre-apply
SQLite initialization probe with the same bounded path/sentinel audit, reduced
the measured median without removing any view, path audit, operation lock,
fact, or gate. Ordinary setup is now three explicit actions: apply plan,
confirm scope/baseline, and verify patch; review, task acceptance, gate,
readiness, and delivery remain separate.

## P0-3 Checkpoint

- One immutable operator envelope now owns `state`, canonically ordered
  blockers, legal actions, and complete details. Runtime `status`, `doctor`,
  and `quickstart status` render exactly three lines by default, preserve the
  complete human report under `--verbose`, and emit one exact JSON object under
  `--json` without a second diagnostic stream.
- The specialized `kafa project doctor` keeps its Python, Git, project-root,
  runtime, gitignore, and local-only checks. Its presentation adapter uses the
  same envelope shape and exit truth; `kafa project status` and quickstart
  modes pass flags through without adding wrapper prose to JSON.
- Sentinel and path checks occur before SQLite. `rollback-incomplete`,
  `recovery-required`, unsafe paths, and an existing unreadable database never
  recommend initialization. SessionStart, SubagentStart, and Stop preserve
  recovery truth even when the database is absent or still present.
- Public `quickstart_status()` / `quickstart_status_lines()` and the CLI now
  use the same pinned runtime snapshot. The canonical delivery evaluator
  captures one current candidate for all structured and policy queries, does a
  final recheck, and returns `candidate-snapshot-changed`, no legal action, and
  non-deliverable trust if source identity changes while the report is built.

The initialized-empty source-runtime measurements were:

| Command | Bytes | Lines | Suggested actions | Baseline / result |
| --- | ---: | ---: | ---: | --- |
| `status` | 396 | 3 | 1 | exact three-line contract passed |
| healthy `doctor` | 40 | 3 | 0 | exact three-line contract passed |
| `quickstart status` | 396 | 3 | 1 | 3,392 -> 396 bytes, 88.33% reduction; <=848-byte budget passed |

The P0-3 red suite initially produced 20 failed assertions across 10 tests.
After implementation and adversarial fixes:

- the final main P0-3 cold-start, doctor, recovery, quickstart, wrapper, Hook,
  Store, path-safety, and operation-lock command ran 88/88 tests successfully
  with `ResourceWarning` treated as error and no skips;
- the delivery/candidate/trust regression selection ran 122/122 successfully;
  this overlaps the operator and Hook evidence and is not added to the 88;
- deterministic fixture E2E initially exposed one stale long-status
  expectation (5/6), which was corrected to consume JSON details; the rerun
  passed 6/6 with zero skips and zero false passes;
- a freshly built wheel and source distribution passed isolated venv/HOME
  installation, plugin/cache discovery, schema-30-to-31 dry-run and migration,
  doctor, quickstart, Hook, uninstall, and artifact checks with
  `artifact_mode=true` and `ok=true`. The temporary wheel SHA-256 was
  `a732d0e6d3bddaccabf90656109df73404bb6f7fea8d78eff88c4abcb52850b3`;
  the temporary source archive SHA-256 was
  `ed0a0898ea18dd440b9a12971ae731b66d87c6a0b6da49380902727cb5a73976`.

Independent P0-3 QA ran 204/204 overlapping presentation, wrapper, Hook,
Store, delivery-policy, stop-ship, and integrity checks with no skips. It first
found one High candidate-snapshot race and Medium gaps in verbose output, Hook
recovery reporting, wrapper unreadable-state handling, and the public
quickstart read API. Main added deterministic regressions and fixed each item;
the final re-review reports zero open Critical, High, or Medium findings.

## P0-4 Checkpoint

- The initial eight-test narrative contract produced one pass, four assertion
  failures, and three errors before production changes. The final adversarial
  suite expanded to 29 deterministic cases and passed 29/29 with
  `ResourceWarning` treated as error.
- `record_delivery()` now persists the exact sorted acceptance set returned by
  the canonical prerequisite report. Caller acceptance, validation, QA,
  failure-mode, changed-file, and quality-gate prose remains accepted only as
  labelled supplemental audit text and cannot create an authoritative link.
- The immutable delivery-ID read model derives requirement, acceptance,
  eligible accepted-task, qualification, execution, validation, failure-mode,
  finding, gate, decision, trust, cycle, candidate, and event-recorded-time
  facts. Historical projection renders every delivery in immutable-event order
  and does not absorb facts from the current cycle.
- Validation evidence is split into policy-eligible execution evidence,
  execution-linked but ineligible evidence, and judgment-only records. Artifact
  tamper, stale qualification, wrong target identity, and medium-or-higher
  regex failure-mode coverage cannot remain authoritative.
- Changed files are derived only when the base is a full immutable local commit,
  the candidate is clean and exactly HEAD-comparable, rename behavior is fixed,
  paths are strict UTF-8, and candidate/index/HEAD observations remain stable.
  Non-UTF-8 collisions, ignored canonical source, mutable bases, and observed
  TOCTOU changes return `unknown/not derivable`; they never fabricate `none`.
- Persisted decision and derived trust are separate. Both current
  `validate --delivery` and historical audit reuse one decision/trust check.
  Accepted-risk relabelling, multiple delivery rows, or a sole delivery row
  without its immutable `delivery_recorded` event fail closed; trust time and
  projection order use the immutable event timestamp rather than mutable row
  time.
- The final eight-module delivery, trust, projection, schema, migration,
  lifecycle, and feature-freeze selection ran 273/273 with zero failures,
  errors, skips, expected failures, or ResourceWarnings in 113.242 seconds
  (113.40 seconds wall time). Renderer check, OpenSpec validation, and
  `git diff --check` also passed.
- Two independent read-only QA passes closed every reported narrative,
  projection, Git-derivation, trust, and delivery-ID finding. Their final state
  is zero open Critical, High, or Medium findings. Overlapping QA runs included
  29/29 narrative, 133/133 delivery-integrity/trust, 63/63
  schema/migration/local-core, and 5/5 projection/path-safety tests; these are
  not added to the 273 total.
- A fresh initialized runtime reports schema 31 and exactly 30 product tables;
  the source distribution contains 18 public JSON schemas. Schema and migration
  production files have no P0-4 diff, and 27/28/29/30-to-31 tests prove that
  migration creates no qualification, gate, outcome, or narrative authority.
- Delivery CLI help marks all six compatibility prose flags as supplemental.
  Scope/rationale, risk decisions, data/config exceptions, known gaps, and
  handoff remain the explicit human-judgment surfaces.
- At this P0-4 checkpoint, persistent real-Native evidence remained separately
  stale because its source digest predated the work. It was not called passing;
  the P0 exit section below records the later authorized refresh.

## P0 Exit Gate

The combined P0 contract matrix ran 327/327 tests successfully in 184.392
seconds (184.71 seconds wall time), with zero failures, errors, skips,
expected failures, unexpected successes, or `ResourceWarning`. It covered the
workflow source, transactional plan, verified patch, concise presentation,
derived narrative, delivery-integrity P0/P1, local trust, schema-30/31
migration, and feature-freeze contracts.

The first complete discovery intentionally remains part of the evidence trail:
874 tests produced 859 passes, one failure, and 14 skips. The only failure was
the documentation contract correctly rejecting stale persistent Native reports.
After refreshing both real profiles, the final complete discovery ran 874 tests
in 472.531 seconds (473.59 seconds wall time): 860 passed, 14 skipped, zero
failed/error, zero expected failure/unexpected success, and zero
`ResourceWarning`. The 14 skips remain skips and are not included in the pass
count.

Auxiliary P0 evidence is accounted separately:

- workflow rendering, strict OpenSpec validation, structure validation, and
  `git diff --check` passed;
- 33 JSON documents parsed with duplicate-key rejection, 18 public schemas
  remained present, and the selected schema semantic contracts passed 42/42;
- documentation contract passed 22/22 after the Native refresh;
- runtime smoke passed 2/2 real local scenarios;
- fixture E2E passed 6/6 and stability E2E passed 11/11 under
  `deterministic-local-runtime`, with zero skip, false pass, human intervention,
  or SQLite lock error. Fixture E2E remains fixture-scoped rather than Host
  evidence;
- Skill eval proved 23/23 deterministic markers, but `CODEX_EVAL_CMD` was not
  set, so this result is explicitly fixture-only.

The authorized real Native single and parallel profiles used only synthetic
prompts and temporary synthetic repositories. Both reports bind the same
current executable identity:

```text
workspace_sha256 f7f2a937722e7488d738d63dd3369bb60a4587cb6669cdd3257ab7a7238f2f3e
status_sha256    e08f3a3ff8533bbc3aac7bb77d0ce062d7000498f4c8c3912ca97de62cf92ab2
native_binary    d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406
```

| Native profile | Result | Tokens | Agent runtime | Scenario duration |
| --- | --- | ---: | ---: | ---: |
| single | 1/1 passed; 0 failed/skipped/false-pass/human-intervention | 51,943 | 32.562258 s | 34.552984 s |
| parallel | 1/1 passed; 0 failed/skipped/false-pass/human-intervention | 103,526 | 37.275089 s | 42.175144 s |

The P0 measurements use two independent, non-concurrent seven-sample benchmark
groups. Only the established 50 ms mutation limit is a numeric timing gate:

| Metric | Group A median | Group B median | Truthful status |
| --- | ---: | ---: | --- |
| Fresh init | 0.155877 s | 0.165115 s | measured/comparative; no numeric gate |
| 5k-fact mutation | 0.017662 s | 0.017872 s | both pass the <=0.050 s gate |
| Targeted three-view projection | 0.007666 s | 0.007889 s | measured/comparative |
| Full 13-view projection | 0.036113 s | 0.035874 s | measured/comparative |

Fresh schema-31 DB size remains 380,928 bytes and 30 product tables. Eleven
cold subprocess samples produced 0.093453 seconds median for CLI `--help` and
0.161482 seconds for initialized `status`; neither has a numeric gate. The
benchmark report's own `full_test` field remains `not-run`; the separate full
discovery above is not injected into or misrepresented by that field.

| Surface metric | Baseline | P0 exit | Result |
| --- | ---: | ---: | --- |
| Independently maintained workflow lists | 7 | 1 | -85.71% |
| Seven guidance files, physical bytes | 69,620 | 49,508 | -28.89% |
| Conservative maintained guidance (`manual remainder + contract`) | 69,620 | 40,871 | -28,749 / -41.29%; >=40% target passed |
| Runtime parser nodes | 59 | 61 | +2 for `delivery-plan` and `verified-patch` |
| Plan/setup actions | 10 | 3 | plan apply, baseline confirm, verified patch |
| Plan graph transactions | multiple fact writes | one `BEGIN IMMEDIATE` | graph-only; no lifecycle/gate/delivery advancement |
| Plugin payload, caches excluded | 1,333,527 B / 71 files | 1,464,500 B / 74 files | +130,973 B; old 1 MiB budget exceeded by 415,924 B |
| Fresh DB | 380,928 B | 380,928 B | unchanged; old 320 KiB budget still exceeded by 53,248 B |

The conservative guidance measurement counts 29,947 bytes of non-generated
document content plus the one 10,924-byte canonical contract. The eight bounded
generated blocks remain physical readable views, but not independently edited
policy sources. The plugin and historical DB size deviations are reported, not
called passing; no trust, migration, path-safety, or gate authority was removed
to recover those retired byte budgets.

At the fixed root `/private/tmp/kafa-lwl-p0-metrics-root`, initialized `status`
was 354 bytes / three lines / one action, healthy `doctor` was 40 bytes / three
lines / zero actions, and initialized-empty `quickstart status` was 354 bytes /
three lines / one action. The quickstart reduction is 89.56% from 3,392 bytes
and passes the 848-byte limit. Absolute command paths make byte counts
root-length-sensitive, so the fixed root is recorded as part of the metric.

`delivery record` still accepts ten compatible prose arguments: one required
judgment field (`scope`), six optional legacy supplemental fields, and three
optional explicit judgment fields (`data-config-notes`, `known-gaps`, and
`handoff`). Routine authority no longer requires caller prose. The design's
five judgment groups also include risk decisions stored in structured
failure-mode/finding/gate facts; `handoff` remains optional at the parser layer
and is not falsely described as mandatory.

### Main-model adversarial review

| Attack axis | Evidence and result |
| --- | --- |
| Hidden phase advancement | Plan postconditions preserve project/cycle phase and active-cycle status, leave the generated task planned and scope unconfirmed, and compare forbidden fact counts. Verified patch rechecks the same lifecycle surfaces. No C/H/M finding. |
| Partial transaction | Non-gateable target, late qualification failure, missing final relation, and corrupted generated task state all roll back the complete graph. Post-commit projection failure leaves one complete detectable graph and a supported rebuild path. No C/H/M finding. |
| Retry drift | The immutable apply event binds cycle, normalized plan ID, semantic digest, and the complete persisted graph. Exact replay changes no revision, event, task state, or projection byte; conflicting replay fails before mutation. No C/H/M finding. |
| Fabricated verified receipt | The envelope is reconstructed from the new immutable execution and validation created by `verify_run()`, verifies qualification/target/candidate identity, and performs a final candidate recheck. It creates no receipt authority and reports task/gate/delivery status explicitly. No C/H/M finding. |
| Concise-output omission | The default uses the canonical first blocker and one legal action; verbose/JSON retain all blockers/details. Recovery and unreadable-state paths suppress initialization advice. No C/H/M finding. |
| Prose authority | Delivery links equal the evaluator's proven acceptance set; legacy text is quoted under a supplemental heading, cannot inject headings or relations, and changed-file derivation fails closed when Git facts are not comparable. No C/H/M finding. |
| Gate bypass | Readiness, recording, current delivered consistency, and historical audit reuse canonical prerequisite/trust checks, exact delivery rows/events, and current candidate identity. Medium/high risk and accepted-risk paths remain fail closed. No C/H/M finding. |

Two residual Low operational trade-offs remain explicit: projection publication
occurs after the atomic fact commit and can require a supported rebuild, and the
three-line default intentionally hides lower-priority blockers that remain
available in verbose/JSON. Neither condition changes delivery eligibility.

## P1 Advanced-Mode Byte Checkpoint

The default single-producer path now loads the 12,104-byte entry Skill and zero
required delegation-reference bytes. It therefore passes both the 12,800-byte
entry limit and the 16,000-byte entry-plus-required-default-reference limit.
The 3,146-byte full delegation matrix is trigger-only; even when parallel
fan-out, shared-file integration, or explicit advanced review selects it, the
combined 15,250 bytes remain below 16,000. These are UTF-8 byte measurements,
not token estimates, and the trigger-only matrix is not relabelled as a default
dependency.

The P1 advanced-mode targeted matrix then ran 121/121 tests in 74.250 seconds:
zero failures, errors, skips, expected failures, or `ResourceWarning`. It
covered workflow source/generation, documentation and delegation, concise state
visibility, control-plane architecture, local-core and Native Host ownership,
feature freeze, and install/release contracts. Structure validation, renderer
check, and `git diff --check` passed. Contract-derived Skill eval passed 30/30
markers and remains explicitly fixture-scoped. Deterministic fixture E2E passed
6/6 and stability E2E passed 11/11, with zero failed, skipped, false-pass,
human-intervention, or SQLite-lock scenarios.

Real Native compatibility was refreshed separately for the same current source
identity: `workspace_sha256=bbfc83a07726a3a71ae713a77c0c705fc3b13621342ff8caf99ee42eb4e7b81d`
and `status_sha256=33c5be88e6777f5d7c72eabcbd9662b08070a371904ba989f4e592f1a785fd46`.
Single passed 1/1 with 50,805 tokens and 76.215834 seconds agent runtime;
parallel passed 1/1 with 104,008 tokens, 73.108130 seconds agent runtime, and
53.242122 seconds observed producer overlap. Both used the current Native binary
digest `d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.

The initial P1 exit matrix ran 252/252 entrypoint, inventory, delegation,
state-card, trigger, documentation, install, evaluator, Hook, schema, and
guided-loop tests in 251.677 seconds with zero failure, error, skip, expected
failure, or `ResourceWarning`. This green matrix is not the P1 exit decision:
the independent review below identified coverage gaps that remain stop-ship
until task 10.5 adds red tests, fixes them, and reruns the affected and combined
gates.

Complete discovery first exposed one stale fixed-list test consumer (886 pass,
one error, 14 skip), then correctly exposed stale Native evidence after that
test repair (886 pass, one failure, 14 skip). Neither run is called passing.
After manifest-deriving the consumer and refreshing real Native evidence, the
final P1 discovery ran 901 tests in 434.053 seconds: 887 pass, 14 skip, zero
failure/error, zero expected failure/unexpected success, and zero
`ResourceWarning`.

The explicit auxiliary gate also passed runtime smoke 2/2, fixture E2E 6/6,
stability E2E 11/11, structure validation, workflow rendering, 34 JSON documents
with duplicate-key rejection, and 46/46 documentation/schema checks.
`git diff --check` passed. Skill eval matched 30/30 deterministic markers, but
its default source was the local fixture, so this observation remains
fixture-only and is not a fresh Host or advanced-evidence pass.

The P1 budget rerun used five 5k-fact samples and eleven cold subprocess
samples. The 5k-fact mutation median was 0.019206 seconds and passed the only
numeric timing gate of <=0.050 seconds. Fresh init was 0.162230 seconds,
targeted three-view projection 0.017216 seconds, and full 13-view projection
0.036964 seconds; these are comparative measurements without numeric gates.
The benchmark report's `full_test` remains `not-run`; the separately completed
901-test discovery is not injected into that field.

| P1 surface metric | Current | Status |
| --- | ---: | --- |
| Source plugin payload, caches excluded | 75 files / 1,480,137 B | old 1 MiB budget exceeded by 431,561 B; deviation retained |
| Installed managed plugin | 66 files / 695,552 B | installed `2.0.0-beta.1`, not current source; deployment freshness not called pass |
| Codex cache | 66 files / 695,552 B | same older installed artifact; not current-source evidence |
| Manifest inventory | 7 Skills / 3 Hook events / 3 Native templates / 18 schemas / 20 core / 7 scripts / 3 references / 22 domains | exact source manifest |
| Entry Skill | 12,104 B | <=12,800 passed |
| Entry plus required default references | 12,104 B | zero default reference bytes; <=16,000 passed |
| Triggered entry plus delegation matrix | 15,250 B | <=16,000 passed |
| Runtime parser nodes | 61 | baseline 59, +2 transactional commands; no numeric gate |
| Cold `--help` median | 0.089131 s | comparative only |
| Cold initialized `status` median | 0.152514 s | comparative only |
| Fresh DB | 380,928 B | old 320 KiB budget exceeded by 53,248 B; deviation retained |

At ephemeral root
`/var/folders/5q/mvhdgpc50q9g60mdr59z_v1w0000gn/T/kafa-p1-exit-metrics-iv4kfjrb`,
initialized `status` and `quickstart status` were each 403 bytes, three lines,
and one action; healthy `doctor` was 40 bytes, three lines, and zero actions.
The root path affects absolute command bytes. Both 403-byte initialized-empty
views pass the 848-byte limit.

### P1 adversarial review

Two independent read-only reviews plus main-model source inspection found two
High and six Medium stop-ship defects. Public doctor could omit full database
doctor facts; execution still had a post-validation file-open window; the entry
Skill under-exposed deep triggers; fixture Skill eval accepted contradictory
prose; doctor blocker order could put `.gitignore` before integrity; Hook and
template lists still had secondary hard-coded sources; App Server discovery did
not require exact cache wiring; and recursive undeclared runtime files escaped
inventory checks. The concrete acceptance criteria are recorded as
`LWL-P1-F1` through `LWL-P1-F8` in the issue matrix. No P1 item is closed by the
green regression above while these findings remain open.

## Narrative Baseline

`delivery record` currently accepts `scope` plus nine optional prose fields:
acceptance, changed files, validation, QA, failure-mode coverage, quality gate,
data/config notes, known gaps, and handoff. Acceptance prose is also parsed to
populate `delivery_acceptance`, even though active acceptance and eligibility
already exist as structured facts. The lightweighting target is to derive
authority and relations from facts while preserving legacy prose only as
supplemental notes.

## Locked Comparison Metrics

Final comparison must report, without hiding deviations:

1. workflow/guidance bytes and independently maintained list count;
2. public CLI/parser, schema/table, Skill/Hook/template inventory;
3. plan setup command count and transaction count;
4. status/doctor/quickstart default bytes, exact lines, and suggested actions,
   with the 25% byte reduction applied to initialized-empty quickstart;
5. mandatory versus supplemental delivery prose fields;
6. scoped implementation LOC and plugin/artifact size;
7. 5k-fact mutation, plan apply/no-op, startup, projection, and delivery
   evaluator timing;
8. complete test, E2E, install, Native, and remote-CI states with not-run kept
   distinct from pass.

## P2 Metrics Checkpoint

The P2 measurement pass found a real performance regression before closure.
Two independent seven-sample groups put delivery-plan apply at 0.051579 and
0.051659 seconds, above the established 0.050-second budget. Profiling showed
that projection publication computed the complete candidate identity three
times even when no validation could cover a failure mode. A red test first
proved that an empty validation set must not request candidate identity. The
fix performs a bounded `select 1 ... limit 1` existence check, computes the
candidate only when coverage candidates exist, and retains the original
candidate-filtered query. A second regression proves that a stale candidate
validation still produces blank derived coverage and computes identity once.

The final independent seven-project measurement seeded 5,000 decisions in
every project and preserved `changed=true` for apply and `changed=false` for
exact replay:

| Delivery-plan metric | Samples (seconds) | Median | Status |
| --- | --- | ---: | --- |
| Apply plus affected projections | 0.025468, 0.025633, 0.024525, 0.029330, 0.024552, 0.024278, 0.024296 | 0.024552 | passed `<=0.050` budget |
| Exact replay | 0.009787, 0.009738, 0.009819, 0.009765, 0.010140, 0.009705, 0.009832 | 0.009787 | comparative; no writes |

The affected delivery-plan, medium-risk, local-policy, and narrative suites
passed 126/126 in 41.936 seconds with zero failure, error, or skip and with
`ResourceWarning` treated as error. An earlier independent reviewer measured
0.023807 seconds apply and 0.009591 seconds replay and identified the need for
the bounded existence query; the main-model rerun above is the evidence after
that scale-safe correction.

The local-core five-sample report at
`/private/tmp/kafa-lwl-p2-local-core-benchmark.json` records:

| Local-core metric | P2 median | Status |
| --- | ---: | --- |
| Fresh init | 0.165984 s | comparative |
| Fresh schema-31 DB | 380,928 B | unchanged; historical 320 KiB deviation retained |
| 5k-fact mutation | 0.017195 s | passed `<=0.050` budget |
| Targeted three-view projection | 0.007233 s | comparative |
| Full 13-view projection | 0.035536 s | comparative |

The report's own `full_test` remains `not-run`; the separately executed full
discovery is not injected into that field.

| Lightweighting metric | Baseline | P2 checkpoint | Result |
| --- | ---: | ---: | --- |
| Seven physical guidance files | 69,620 B | 53,759 B | -22.782%; physical readability retained |
| Conservative independently maintained guidance | 69,620 B | 41,305 B | -40.671%; `>=40%` target passed |
| Entry Skill | 12,711 B | 12,640 B | passed `<=12,800` |
| Entry plus required default references | 12,711 B | 12,640 B | zero default reference bytes; passed `<=16,000` |
| Triggered entry plus delegation matrix | 14,303 B | 15,786 B | advanced-only; passed `<=16,000` |
| Main five implementation surfaces | 10,166 LOC | 14,481 LOC | +4,315; explicit deviation, not called lightweight |
| Source plugin payload, caches excluded | 71 files / 1,333,527 B | 75 files / 1,496,113 B | +162,586 B; old 1 MiB deviation retained |
| Initialized-empty quickstart default | 3,392 B / 23 lines / 8 actions | 410 B / 3 lines / 1 action | -87.913%; passed `<=848 B` and one-action gates |
| Initialized status default | 192 B / 11 lines | 410 B / 3 lines / 1 action | byte count increased; exact concise-card contract passed |
| Healthy doctor default | 26 B / 1 line | 40 B / 3 lines / 0 actions | byte count increased; exact concise-card contract passed |
| Cold `kafa --help` | not gated | 0.095799 s median / 11 samples | comparative |
| Cold initialized `status` | not gated | 0.167615 s median / 11 samples | comparative |

The conservative guidance figure is the 29,841-byte manual remainder plus the
single 11,464-byte workflow contract. It does not count generated prose blocks
as independently maintained sources. The Host exposes no isolated Skill-token
counter, so UTF-8 bytes are reported directly rather than converted into
invented tokens. The selected real Native observations were 51,889 tokens for
single and 103,944 for parallel; prompt, cache, and model variability make
these observations non-causal rather than a token-reduction claim. Their
detail is now historical because later source edits changed the candidate.

An exact-HEAD metrics build, created locally without install or publication,
produced:

| Artifact | Baseline bytes / SHA-256 | P2 bytes / SHA-256 | Delta |
| --- | --- | --- | ---: |
| Wheel | 48,085 / `8edddff934c199a01379dc99757b212e50903b36f4236b8eccd49bbbbd3f3d6e` | 70,718 / `c1ccf29c268dac3c28a3483cfb41cd68d7ffaa2fda1846cafe7c2648414decc9` | +47.069% |
| Source archive | 502,068 / `38968ab54c794aa4f6965bc6cdff986b813a75ad801f04db7565ac582a0be37a` | 595,091 / `93fe081bda81f7c4cebd9bf5fea435df904a4b5c1a444191d6ffce56e6a4c103` | +18.528% |
| Combined | 550,153 | 665,809 | +21.023% |

This is an explicit size deviation, not a passed lightweighting metric. The
build only supports the comparison checkpoint; a fresh artifact/supply-chain/
isolated-install run remains required after the final source review.

Stable evidence summaries materially reduce the default review surface while
remaining digest-bound to retained detail:

| Evidence/output surface | Detail/baseline | Summary/P2 | Reduction |
| --- | ---: | ---: | ---: |
| Tracked Native single plus parallel | 10,448 B | 2,331 B | 77.690% |
| P2 temporary Native single plus parallel | 10,446 B | 2,406 B | 76.967% |
| No-window outcome benchmark | 10,080 B | 8,280 B | 17.857% |

Summary-only input cannot prove pass: missing, stale, digest-mismatched,
fixture-substituted, semantically invalid, or falsely current detail remains
fail-closed. The outcome reduction replaces six synthetic null/not-run metric
objects with `field_metrics_status=not-observed`; it does not fabricate zeros,
and observed field windows retain the complete six-metric contract.

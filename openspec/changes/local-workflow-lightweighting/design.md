## Context

Kafa is already a schema-31, local-only verified delivery kernel. Its safety
model is intentionally strict: Native Codex/ChatGPT owns collaboration
lifecycle, the root controller is the only fact writer, executions are
immutable and candidate-scoped, and one prerequisite evaluator fails closed
before readiness or delivery.

The current weight is primarily representational and operational rather than a
missing data model. README, QUICKSTART, the entry Skill, trigger documentation,
the full-flow example, and Skill-eval fixtures each restate parts of the same
workflow. `quickstart minimal` reduces typing but performs several independent
mutations and optionally executes immediately, so a late failure can leave a
partial graph. Human status output exposes internal phase/checklist detail by
default. Delivery rows also retain prose that restates acceptance, validation,
failure-mode, and gate facts already present in normalized relations.

This change is based on `main@7c7aa41929426bc1d89350497ceb2c9266290b88`
and `Kafa_Lightweighting_Deep_Analysis_20260722.md`. Schema 31, its 30 product
tables, the package release state, and the installed user plugin remain
unchanged.

## Goals / Non-Goals

**Goals:**

- make one machine-readable presentation contract the source for workflow
  ownership, routes, stage dependencies, command examples, and advanced-mode
  triggers;
- reduce a single-patch planning graph to one atomic command and verification
  to one explicit immutable-execution command without hiding baseline, task,
  review, readiness, or delivery boundaries;
- make default human status answer only state, top blocker, and next action,
  while retaining complete verbose and JSON evidence;
- derive routine delivery narration and acceptance links from normalized facts,
  leaving prose for actual judgment, exception, unknown, and handoff content;
- reduce ordinary entrypoint, inventory, advanced-mode, release-evidence, and
  missing-metric maintenance friction after the P0 contract is green;
- measurably reduce command count, duplicated workflow text, default output
  bytes, and hand-written delivery prose without weakening any blocker.

**Non-Goals:**

- no schema 32, database migration, new business table, or second fact source;
- no automatic semantic inference that a target proves an acceptance;
- no automatic task acceptance, quality gate, readiness, delivery, commit,
  push, release, deploy, production migration, or user-plugin replacement;
- no removal of audit, retrospective, live-host, SBOM, provenance, rehearsal,
  or outcome capabilities;
- no runtime dependence on OpenSpec documents or Markdown projections;
- no change to high/critical `human-review-required`, medium-risk coverage,
  candidate identity, path safety, or current-candidate eligibility.

## Decisions

### 1. A presentation contract is the single workflow source, not the gate

Add
`plugins/codex-project-harness/references/workflow-contract.json` with a closed,
versioned shape for:

- authority owners and exclusions;
- invariant safeguards;
- Skill routes and advanced-mode triggers;
- workflow stages and dependency edges;
- public command templates;
- concise output labels and handoff obligations.

The workflow is a dependency graph, not a falsely rigid transcript. In
particular, task submission and controller verification may occur in either
order, but both must precede task acceptance; qualification precedes
verification and gate review; acceptance, gate, readiness, recording, and final
validation remain ordered.

A repo-only `tools/render_workflow_docs.py` renders bounded, marker-delimited
blocks and supports `--check`. It updates the README overview, QUICKSTART happy
path, `project-harness` Skill obligations, trigger matrix, full-flow example,
and Skill-eval material. Hand-written content outside generated blocks is
limited to document-specific explanation and explicit exceptions. Tests reject
drift, missing dependencies, duplicate hand-written workflow lists, and a
non-idempotent render.

The separation of authorities remains explicit:

- OpenSpec is the normative product-spec authority;
- `core.delivery.evaluate_delivery_prerequisites` is the executable gate
  authority;
- SQLite is runtime fact authority;
- `workflow-contract.json` is presentation/routing authority only.

The contract never drives or relaxes delivery eligibility. A contract/runtime
alignment test checks required stage and blocker names, but runtime code does
not trust editable prose.

Alternative rejected: generate documentation directly from `core/delivery.py`.
That would mix policy presentation with a security-sensitive evaluator and
still would not describe Skill routing. Markdown as the source was also
rejected because it recreates hand-parsing and cannot reliably drive drift
checks.

### 2. One fact transaction applies a delivery plan

Add a closed version-1 plan object containing:

```json
{
  "version": 1,
  "id": "PATCH",
  "goal": "...",
  "acceptance": "...",
  "task": "...",
  "test": {"kind": "unit", "command": "..."},
  "failure_mode": null
}
```

`quickstart delivery-plan --file <json>` and the equivalent explicit core API
perform complete preflight, generate cycle-aware IDs, and write requirement,
acceptance, optional failure mode, task, target, qualification, relations, and
audit events inside one `BEGIN IMMEDIATE` transaction. The task remains
`planned`; scope remains `unconfirmed`; no execution, validation, gate,
delivery, or phase advancement is created. The command requires an initialized
project so initialization remains a separate, recoverable boundary.

Plan application has exact replay semantics:

- a byte-equivalent logical plan is a no-op with no revision, event, timestamp,
  or projection change;
- any same-ID semantic conflict fails before the first write;
- an invalid final relation or qualification rolls the entire graph back;
- a closed cycle fails before mutation;
- projection publication occurs once after the fact transaction. Projection
  failure cannot create a partial fact graph and remains detectable/rebuildable
  under the existing derived-view contract.

The implementation uses connection-scoped internal primitives shared with the
existing public mutations; it does not call the public one-transaction-per-fact
functions in sequence. No `delivery_plans` table is added because the normalized
facts and their audit events are the plan authority.

Alternative rejected: a thin alias around existing commands. It is shorter but
not atomic and has inconsistent retry behavior. A schema-32 plan table was
rejected because it duplicates the graph and creates migration cost.

### 3. `verified-patch` is an explicit verification result, never delivery

Add `quickstart verified-patch --id <plan-id>` as a convenience over the
existing controller-owned `verify_run()` transaction. It resolves only the
current generated acceptance, qualification, and target mapping, runs the
registered target, and returns a structured envelope derived from the newly
persisted immutable execution and validation:

```json
{
  "kind": "verified-patch",
  "verification_status": "pass",
  "task_status": "planned",
  "gate_status": "not-run",
  "delivery_status": "not-run",
  "cycle_id": "...",
  "candidate_sha": "...",
  "qualification_id": "...",
  "target_id": "...",
  "target_definition_sha256": "...",
  "execution_id": "...",
  "validation_id": "..."
}
```

The existing `verify run` CLI and `(execution_id, validation_id)` Python return
contract remain compatible. The new envelope is a read model, not a receipt
table. If candidate, acceptance revision, target digest, or qualification is
stale, verification fails closed before a passing envelope. Generating the
envelope never starts, submits, accepts, or cancels a Kafa task and never writes
a gate, readiness phase, or delivery.

Baseline confirmation remains an explicit root-controller command between plan
application and eventual readiness. This yields a three-command setup path:
apply plan, confirm scope, verify patch. Review, gate, readiness, and delivery
remain separately visible.

Alternative rejected: make a command that also accepts the task and records a
gate. That would turn a convenience entrypoint into a trust bypass.

### 4. Concise human output is a projection of the complete report

Introduce a shared presentation envelope with:

- `state`;
- ordered `blockers` using existing stable blocker codes;
- ordered `actions` containing legal commands;
- `details` containing the complete current report.

Default human output for runtime `status`, `doctor`, and `quickstart status`
renders only state, the first blocker (or `none`), and the primary next action
(or `none`). `--verbose` renders all current details and blockers. `--json`
prints one valid JSON object to stdout for success and failure; optional verbose
diagnostics never contaminate JSON stdout. Internal phase names are verbose/JSON
facts, not default operator state.

The first blocker is selected from the canonical evaluator order, not from
alphabetical text. Only the first legal action is recommended. The existing
long human view remains available under `--verbose` to limit compatibility
cost for operators who need it.

Alternative rejected: truncate the existing line list. That can hide the wrong
blocker and leaves status, doctor, and quickstart with divergent logic.

### 5. Authoritative narrative is derived at read time

Add an immutable `DeliveryNarrativeFacts` read model in `core.delivery` (or a
small sibling core module) containing entity IDs and normalized facts for:

- current requirement/acceptance graph;
- accepted task coverage;
- current qualifications, executions, and validations;
- failure-mode coverage and accepted risks;
- latest gate, linked findings, linked qualifications, trust status, cycle, and
  candidate;
- deterministically derivable changed files when a valid Git `base_ref` exists.

`record_delivery()` always writes `delivery_acceptance` from the complete active
acceptance set proven by the prerequisite report. Caller prose can neither add
nor remove authoritative links. Existing delivery prose flags stay accepted for
compatibility but are stored and displayed only under a clearly labelled
`Legacy / Supplemental Notes` section. They cannot override the derived section
or alter readiness.

Scope, unresolved/accepted risk judgment, configuration exceptions, known gaps,
and handoff notes remain human inputs. Changed files are sorted and derived when
the base reference is valid; otherwise the authoritative view says
`unknown/not derivable` and may display a human supplement. It never fabricates
`none`.

No delivery, validation, or quality-gate column is removed and no schema or JSON
schema generation changes. Historical prose remains auditable. Projection
rebuilds derive current/historical narrative from persisted structured facts
and remain byte-stable.

Alternative rejected: concatenate generated prose into the existing columns at
write time. That would keep two copies of the same fact and allow later drift.

### 6. P1 normalizes entrypoints, inventory, and advanced modes

After all P0 checkpoints pass:

- ordinary project commands are documented and exposed as `kafa project ...`;
  plugin-internal Python paths remain maintainer-only. Project subcommand
  passthrough is derived from the public runtime-domain inventory rather than a
  second hand-written parser list;
- a versioned
  `plugins/codex-project-harness/references/distribution-manifest.json` becomes
  the sole inventory for Skills, Hooks/events, templates, schemas, core, scripts,
  and public runtime domains. Source doctor, cache doctor, structure validation,
  install tests, and evaluators read it from the plugin authority they inspect;
- the five-field delegation packet (goal, acceptance, allowed files, exact
  test, escalation) is the default for one producer. The full matrix is loaded
  only for parallel fan-out, shared-file integration, or explicit advanced
  review;
- phase/cycle internals remain available in verbose/JSON output but disappear
  from the default card;
- audit, retrospective, live-host compatibility, and release rehearsal are
  selected only by declared triggers such as multi-day work, repeated escapes,
  schema/runtime changes, milestone review, host integration, packaging, or
  release-surface changes.

This is normalization, not immediate capability deletion. The retained seven
Skills, three Hooks, three templates, 18 public JSON schemas, schema-31 tables,
and all advanced commands remain present.

### 7. P2 makes evidence pressure change-scoped and summaries stable

After P1 checkpoints pass:

- release workflow classification computes an explicit local change scope. A
  live Native profile is blocking only for host integration, packaging,
  release-tooling, or Native-evaluator changes; other changes report it as
  advisory/manual without being relabelled pass;
- checksum, SBOM, provenance, isolated-install, and rehearsal logic share one
  artifact-subject/digest model. Existing verification remains intact;
- stable summary manifests remain in the main review surface. Volatile detailed
  Native/rehearsal proof is generated as CI artifacts or explicitly refreshed
  evidence and is never substituted by fixtures;
- outcome reporting emits one `field_metrics_status=not-observed` sentinel when
  no bounded field window exists instead of expanding multiple null/not-run
  values. When observations exist, every metric retains its full numerator,
  denominator, window, and missing-data semantics.

No release capability is removed. A blocking profile that is selected and is
unavailable remains blocked/not-run; path scoping cannot convert it to pass.

### 8. Performance and token budgets are explicit

The implementation must satisfy:

- single-plan apply is at most the existing 0.050-second 5k-fact mutation budget
  on the established benchmark host;
- exact no-op replay performs no fact writes or projection publication;
- every default status/doctor/quickstart human output is exactly the three
  state/blocker/next lines and contains no more than one suggested command;
- initialized-empty quickstart output is at most 25% of its 3,392-byte
  baseline; status and healthy doctor retain separate byte measurements because
  the required three labels cannot fit within 25% of their smaller baselines;
- generated workflow blocks reduce independently maintained workflow/gate lists
  to one contract and reduce aggregate duplicated bytes by at least 40%;
- a minimal delivery record requires only scope/handoff plus genuine exception
  or unresolved-risk prose; all other routine narrative is derived;
- plugin size and cold-start budgets remain within the existing accepted limits,
  or any deviation is reported rather than hidden.

## Risks / Trade-offs

- **[Presentation contract becomes a second gate]** → runtime never consumes it
  for eligibility; alignment tests are one-way assertions against the evaluator.
- **[Generated docs become unreadable or overwrite context]** → only bounded
  marked blocks are generated; document-specific prose remains outside them;
  render is deterministic and `--check` is mandatory.
- **[Plan helper diverges from individual mutations]** → both paths share
  connection-scoped primitives and the same guards, digests, events, and
  invariants; combined tests compare their resulting logical graph.
- **[Retry changes revisions or invalidates qualification]** → full preflight
  and exact logical digest make identical replay a true no-op.
- **[The name verified-patch is mistaken for delivered]** → every envelope
  explicitly includes task, gate, and delivery status; none is inferred.
- **[Concise output hides important diagnostics]** → first blocker remains
  fail-closed and actionable; `--verbose` and `--json` retain all details.
- **[Legacy prose contradicts facts]** → it is visibly supplemental and cannot
  populate authoritative relations or override derived narrative.
- **[Change-scope classification skips needed live evidence]** → the classifier
  is closed, conservative, unit-tested, and defaults unknown paths to blocking.
- **[P1/P2 expands the initial change too far]** → no P1 production edit begins
  until every P0 task, regression checkpoint, and adversarial review is green;
  P2 has the same dependency on P1.

## Migration Plan

There is no database migration. `SCHEMA_VERSION` remains 31, greenfield remains
30 product tables, and existing schema-27/28/29/30-to-31 migration behavior is
regression-tested unchanged.

Rollout is additive and reversible:

1. land the workflow contract and drift checker while retaining existing text;
2. convert bounded blocks and prove deterministic regeneration;
3. add red tests, then the atomic plan and verified-patch surfaces;
4. add concise/verbose/JSON presentation while preserving old long output under
   `--verbose`;
5. switch delivery projections to derived authority plus supplemental legacy
   notes;
6. checkpoint all P0 behavior before P1 inventory/entrypoint normalization;
7. checkpoint P1 before release/evidence P2 changes;
8. run the complete regression, install, fixture/stability, benchmark, and
   independent QA gates.

Rollback is ordinary source rollback because no persisted fact is removed or
reinterpreted. Plans already applied consist only of valid existing schema-31
facts. Existing delivery prose columns and CLI flags remain readable. No change
requires downgrading or rewriting a business-project database.

## Open Questions

None. The change intentionally resolves ambiguous choices conservatively: JSON
is the plan format, setup and verification remain separate, schema 31 remains
unchanged, and unknown release/evidence scope defaults to the stricter path.

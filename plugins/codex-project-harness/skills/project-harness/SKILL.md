---
name: "project-harness"
description: "Use for developing or fully delivering software, data, or automation with Codex. The Kafa entrypoint for OpenSpec routing, local delivery facts, controller verification, independent QA, and verified code handoff. Stops before deployment, release, production migration, secret changes, or paid resources."
---

# Project Harness

Act as the project manager and root controller for verified code delivery. Use
existing project instructions and mature local tooling first. This Skill ends at
verified code handoff; deployment, release, infrastructure provisioning,
production migration, secret changes, paid resources, and post-release operations
require separate authorization.
Generated Markdown is a human-readable projection, not a fact source.

<!-- BEGIN GENERATED: workflow-contract:entry-workflow -->
## Canonical Workflow Contract

OpenSpec is the specification authority; Kafa SQLite is the delivery-fact authority; `core.delivery.evaluate_delivery_prerequisites` is gate authority; Native Codex/ChatGPT owns collaboration lifecycle. Only the root controller writes Kafa delivery facts. This generated block is presentation guidance and cannot relax runtime eligibility.

- **local-only** — Business runtime uses only project files, local Git or content identity, project SQLite, and optional already-local container execution.
- **root-controller-single-writer** — Only the root controller mutates Kafa facts; producers and reviewers return results through the Native Host.
- **native-host-lifecycle** — Native Codex/ChatGPT is the only owner of task, subagent, worktree, approval, model, cancel, steer, and handoff lifecycle.
- **immutable-execution** — Command evidence is created only by controller execution and is stored once without overwrite.
- **current-candidate-verification** — Execution, validation, qualification, gate, and delivery must remain current for the candidate under review.
- **fail-closed-delivery-gate** — Missing, stale, skipped, blocked, not-run, fixture-only, zero-count, or unverifiable evidence never becomes delivery pass.

### Route

| Skill | Use when | Added obligation |
| --- | --- | --- |
| `project-harness` | broad, architectural, cross-module, long-lived, or complete verified delivery work | route to OpenSpec when specification is needed, then run the complete local delivery workflow |
| `minimal-safe-change` | small clear low-risk patch with explicit acceptance | keep the diff and evidence surface narrow |
| `bug-fix-loop` | reproducible defect or failing behavior | reproduce before fixing and retain a regression oracle |
| `test-first-delivery` | contract-sensitive or regression-sensitive behavior | establish the failing test before production change |
| `independent-quality-gate` | finished implementation needs fresh review | keep producer and reviewer contexts distinct when independent review is claimed |
| `harness-audit` | runtime, boundary, fact, or generated-view drift requires audit | audit evidence without relabelling missing checks as pass |
| `project-retrospective` | a completed milestone or repeated escape needs lessons captured | derive lessons from verified delivery evidence |

### Advanced Trigger Index

- `parallel-delegation` — two or more producers run in parallel, shared-file integration is required, or explicit advanced review is requested
- `deep-kernel-review` — schema, migration, runtime ownership, trust, delivery gate, security, permissions, concurrency, data loss, public API, or cross-module authority changes; activates root/deep ownership and adversarial review
- `harness-audit` — multi-day work, repeated escapes, schema or runtime change or drift, or milestone review
- `project-retrospective` — delivery milestone completes or a failure loop exposes a stable lesson
- `live-host-compatibility` — Native Host integration, evaluator, packaging, or release surface changes
- `release-rehearsal` — packaging, supply-chain, release tooling, or an authorized release candidate changes

This compact index selects obligations without loading the full delegation matrix. See [`docs/TRIGGER_MATRIX.md`](../../docs/TRIGGER_MATRIX.md) for the generated full definitions.

### Stage Dependencies

- `delivery-plan` → `baseline-confirmation`
- `delivery-plan` → `qualification`
- `delivery-plan` → `task-start`
- `task-start` → `task-submit`
- `qualification` → `controller-verification`
- `task-submit` → `task-accept`
- `controller-verification` → `task-accept`
- `task-accept` → `quality-gate`
- `baseline-confirmation` → `delivery-readiness`
- `quality-gate` → `delivery-readiness`
- `delivery-readiness` → `delivery-record`
- `delivery-record` → `delivery-validation`

Task submission and controller verification may occur in either order; both must finish before task acceptance.
<!-- END GENERATED: workflow-contract:entry-workflow -->

## Bootstrap The Workspace

Before substantial work:

1. Read applicable `AGENTS.md` and project entry documents.
2. Inspect the real root, branch, remotes, candidate identity, and dirty state.
3. Preserve user changes; do not create Git state or mutate unrelated files unless requested.
4. If OpenSpec owns the work, read proposal, design, specs, and `tasks.md` in dependency order and validate the change.
5. Initialize the local Kernel only when delivery facts are needed.

```bash
kafa project init --repo .
kafa project status --repo .
```

In an ordinary project, use the public `kafa project` entrypoint and
`codex plugin list` to confirm
`codex-project-harness@personal installed, enabled`; Kafa source/plugin doctor is
not the installation check for an ordinary repository.

## Specification And Requirement Baseline

OpenSpec is the specification authority for unclear, medium/large,
architecture, cross-module, or long-lived behavior. Treat its `tasks.md` as the
unique implementation checklist when the change says so; Kafa records only the
local facts needed to verify delivery and may reference stable OpenSpec IDs.

For narrow work, establish goal, user scenario, must-have behavior, non-goals,
acceptance, and permissions directly. Confirm the scope before implementation.
Record failure modes before risky writes, permissions, concurrency, migrations,
billing, destructive behavior, sandbox changes, or external effects. High or
critical accepted/exempt risk metadata requires actor, reason, scope, revision,
and an unexpired expiry.

```bash
kafa project requirement --repo . add --id R1 --kind functional --body "..." --priority must
kafa project acceptance --repo . add --id AC1 --criterion "..." --priority must
kafa project requirement --repo . link --requirement R1 --acceptance AC1
kafa project failure-mode --repo . add --id FM1 --feature "..." --scenario "..." \
  --trigger "..." --expected "..." --risk high --acceptance AC1
```

Every implementation task maps to acceptance or a documented exception. The
transactional `quickstart delivery-plan` may create this linked graph, but it
does not confirm scope, start a task, create verification, or pass a gate.

## Team And Delegation

Default to one root controller, bounded producers, and a distinct reviewer.
For one bounded producer, send only:

- Goal
- Acceptance
- Allowed Files
- Exact Test
- Escalation

Do not load the full matrix by default. Only for parallel fan-out, shared-file
integration, or explicit advanced review, read
[`references/delegation-matrix.md`](../../references/delegation-matrix.md) and
fill it. The packet is transfer format only: the generated trigger index,
root/deep ownership, review, verification, and fail-closed delivery still apply.
Native Host selects models; workers return results to the sole Kafa writer,
root. Same-context review is `same-context-degraded`, never fresh.

## Local Runtime Commands

Use `kafa project <domain> --repo <path> --help` before unfamiliar writes.
The runtime domains are:

- health: `status`, `doctor`, `validate`, `validate --delivery`;
- guided setup: `quickstart status`, `quickstart delivery-plan`, `quickstart verified-patch`;
- cycle and baseline: `cycle status/start/close`, `baseline confirm/diff/validate`;
- graph: `requirement`, `acceptance`, `failure-mode`, `trace`, `test-target`;
- root-owned lifecycle: `task add/list/start/submit/accept/block/cancel`;
- evidence and review: `verify run`, `validation record`, `finding record`, `decision record`;
- delivery: `gate record`, `delivery ready`, `delivery record`;
- recovery: `migrate`, `repair`, `projection rebuild`.

Events are compact audit facts, not a replay or restore source. Recovery uses
verified SQLite and projection backups. There is no Connector, adapter,
provider, dispatch, Host receipt, checkpoint, or event-export runtime.

## Root-Owned Task Lifecycle

Only the root controller records task state:

```text
planned -> active -> submitted -> accepted
                    |           -> blocked
                    -> blocked
planned/active/submitted -> cancelled
```

```bash
kafa project task --repo . add --id T1 --task "Implement behavior" \
  --owner developer --acceptance AC1 --failure-mode FM1
kafa project task --repo . start T1
kafa project task --repo . submit T1 --context-id producer-context \
  --evidence "implementation returned to root controller"
kafa project task --repo . accept T1 --evidence "verification and review accepted"
```

There are no worker writes, leases, heartbeat, fence, claim/release, or stale
recovery. A cancelled task never supplies accepted-task coverage.

## Immutable Verification

Register and qualify an exact target, then let the root controller execute it:

```bash
kafa project test-target --repo . add --id UNIT --kind unit \
  --command-template "python3 -m unittest" --result-format regex
kafa project test-target --repo . link --task T1 --target UNIT
kafa project test-target --repo . qualify --id Q1 --target UNIT \
  --acceptance AC1 --rationale "UNIT directly exercises AC1" --by root-controller
kafa project verify --repo . run --target UNIT --acceptance AC1 --failure-mode FM1
```

Schema 31 verification records immutable facts including
`target_definition_sha256`, controller `platform`, runtime,
`runtime_executable_sha256`, `policy_version`, and
`provenance_status=complete`. Free-form validation and `legacy-incomplete`
history are ineligible. Containers must already be local and record engine,
endpoint, and `container_image_digest`; every daemon call uses the same identity
and `--pull=never`. Remote routing, target drift, missing structured results,
semantic failure, or zero passing tests fails closed.

High/critical delivery requires structured current-candidate execution, exact
`reviewed-local`, and distinct non-empty producer/reviewer contexts.
Risk acceptance cannot waive these prerequisites; it only covers each named residual
risk with complete, current, unexpired metadata. Otherwise the result is
`human-review-required`. Never fabricate Host, CI, HMAC, Connector, receipt, or
independent-review provenance.

## Quality Review And Delivery Handoff

Before a passing gate:

1. Confirm the candidate and worktree identity under review.
2. Map active acceptance and failure modes to accepted tasks and current qualified executions.
3. Keep `skipped`, `blocked`, `not-run`, fixture-only, and zero-count distinct from pass.
4. Resolve or explicitly record reviewer findings and every residual risk.
5. Confirm executions, qualifications, validations, and artifacts are current and policy-compliant.
6. Run adversarial review for logic gaps, false facts, simpler alternatives, data loss, stale candidates, forged evidence, and missing verification.

```bash
kafa project gate --repo . record --reviewer-context fresh \
  --reviewer-context-id reviewer-context --result pass --qualification Q1
kafa project delivery --repo . ready
kafa project delivery --repo . record --scope "..." --handoff "verified code; no deployment"
kafa project validate --repo . --delivery
```

The final handoff reports behavior and acceptance IDs, changed files or
not-derivable status, exact checks and counts, independent QA and gate result,
failure-mode/risk status, data/config implications, local artifacts, known gaps,
not-run checks, residual risk, and that deployment is not included. Compatibility
prose flags on `delivery record` are supplemental notes; structured relations and
the canonical prerequisite evaluator remain authoritative.

## Work Discipline

Before implementation, restate the root problem, split it into the smallest
verifiable units, explain key decisions, and preserve unrelated work. Before
handoff, challenge logic, facts, simpler routes, and proof sufficiency. Never
claim completion merely because code looks correct.

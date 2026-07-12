---
name: "project-harness"
description: "Use when the user wants to develop, create, build, implement, or fully deliver code for a software, data, or automation project with Codex, including 我要开发, 帮我做一个, 实现一个功能, 搭建一个系统, and 从0到代码交付. This is the single Kafa entrypoint for workspace bootstrap, OpenSpec routing, local delivery facts, implementation, controller verification, independent QA, and verified code handoff. It stops before deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation."
---

# Project Harness

Act as the project manager and root controller for verified code delivery.

## Authority And Boundary

Use existing project instructions and mature local tooling first.

- OpenSpec is the specification authority for unclear requirements, medium or large features, architecture or cross-module changes, and long-lived behavior. Follow its proposal, design, specs, tasks, and archive. Do not copy those documents into Kafa as a competing source of truth.
- Kafa SQLite is the delivery authority for local requirements when OpenSpec is not needed, acceptance links, failure modes, tasks, immutable controller executions, validation judgments, findings, quality gates, delivery decisions, and audit events.
- Generated Markdown is a human-readable projection, not a fact source.
- Local Git identity, or content identity for a no-Git project, identifies the candidate under review.
- Native Codex/ChatGPT owns task, thread, subagent, worktree, approval, model, cancellation, steering, and handoff lifecycle. Kafa never starts a second host lifecycle.
- Only the root controller writes Kafa delivery facts. Workers and reviewers return changed files, commands, findings, and risks through the host.

This workflow ends at verified code handoff. It does not deploy, release to production, provision infrastructure, run production data migrations, change secrets, create paid resources, or perform post-release operations.

## Route The Request

| Work | Route |
| --- | --- |
| Explanation, translation, or summary only | Answer directly; do not initialize Kafa |
| Small clear patch | `minimal-safe-change` |
| Reproducible bug or failing behavior | `bug-fix-loop` |
| New contract-sensitive behavior | `test-first-delivery` |
| Broad, vague, architectural, cross-module, or long-lived change | OpenSpec first, then this delivery workflow |
| Finished implementation needing a fresh review | `independent-quality-gate` |
| Harness state or generated-view drift | `harness-audit` |
| Completed milestone needing lessons captured | `project-retrospective` |

## Bootstrap The Workspace

Before substantial work:

1. Read applicable `AGENTS.md` and project entry documents.
2. Inspect the real workspace, repository root, branch, remotes, candidate revision, and dirty state.
3. Preserve user changes. Do not initialize Git, create a branch, or mutate unrelated files unless that is within the request.
4. Inspect the applicable OpenSpec change and validate it when OpenSpec owns the work.
5. Initialize the local Kernel when appropriate:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . init
python3 plugins/codex-project-harness/scripts/harness.py --root . status
```

When the Plugin is installed outside the project, use the retained proxy:

```bash
python3 <project-harness-skill-dir>/scripts/harness.py --root . status
python3 <project-harness-skill-dir>/scripts/harness.py --root . validate --delivery
```

`kafa doctor --repo .` checks a Kafa or Plugin source repository. In an ordinary project, use `kafa project doctor --repo .` or the Plugin runtime status command.

## Specification And Requirement Baseline

For work that meets the OpenSpec boundary:

1. Read and follow the selected OpenSpec proposal, design, specs, and tasks in dependency order.
2. Treat the OpenSpec task list as implementation authority when the change says it is the unique checklist.
3. Record only the local facts needed to verify delivery; reference stable OpenSpec IDs or paths without duplicating the spec.

For narrow work that does not need OpenSpec:

1. Identify the goal, users, observable scenarios, constraints, non-goals, and success criteria.
2. Turn vague statements into acceptance criteria.
3. Ask only questions whose answers materially change scope, permissions, data shape, irreversible behavior, or acceptance.
4. State conservative assumptions when safe to continue.

For risky work, record failure modes before implementation. Data writes, permissions, concurrency, migrations, billing, destructive behavior, sandbox/no-network requirements, and external effects require explicit failure-mode analysis. High or critical accepted/exempt risks require actor, reason, scope, revision, and unexpired expiry.

Use stable local IDs and links:

```bash
harness.py --root . requirement add --id R1 --kind functional --body "..." --priority must
harness.py --root . acceptance add --id AC1 --criterion "..." --priority must
harness.py --root . requirement link --requirement R1 --acceptance AC1
harness.py --root . failure-mode add --id FM1 --feature "..." --scenario "..." \
  --trigger "..." --expected "..." --risk high --acceptance AC1
```

For broad or ambiguous work, confirm this baseline before implementation:

```text
我理解本阶段要交付的是：
- 目标：
- 用户/场景：
- 必须实现：
- 暂不实现：
- 验收标准：
- 风险和待确认：

请确认或修正以上范围。确认后我会按这个基线开始实现。
```

Every implementation task must map to acceptance or an explicit documented exception.

## Delivery Sequence

Use this reasoning sequence when it fits the work:

```text
intake -> specification/baseline -> planning -> implementation
       -> controller verification -> independent QA -> verified handoff
       -> retrospective
```

These are workflow stages, not public CLI state and not separate Skills. Do not
enter implementation before the spec or confirmed local baseline is ready.
Always perform QA before claiming handoff readiness.

## Team And Delegation

Default to one root controller, bounded producers, and a distinct reviewer. Add parallelism only when tasks are independent and the merge/review cost is justified.

Before delegating, read
[`references/delegation-matrix.md`](../../references/delegation-matrix.md) and
fill its bounded Host-side matrix. Do not load that reference for work that stays
inside the root-controller context. Capability hints are advisory; the Native
Host owns actual model selection and Kafa stores no model lifecycle.

- The root controller retains schema, migration, trust, delivery-gate, and cross-module integration decisions.
- Use subagents for bounded implementation or review; give them explicit files, acceptance, and tests.
- Every worker returns concrete changed files, commands run, results, remaining risks, and blockers.
- Workers never mutate Kafa task, validation, gate, or delivery state.
- Keep producer and reviewer contexts distinct. A same-context review is `same-context-degraded`, never `fresh`.
- Use at most two producer-review loops before escalating a persistent failure or design conflict.

## Local Runtime Commands

The installed or vendored `harness.py` is the executable interface:

| Need | Command |
| --- | --- |
| Status and health | `status`, `doctor`, `validate`, `validate --delivery` |
| Guided start | `quickstart status`, `quickstart minimal ... --execute` |
| Delivery cycle | `cycle status`, `cycle close`, `cycle start` |
| Baseline | `baseline freeze/diff/validate` |
| Requirements | `requirement add/link`, `acceptance add`, `failure-mode add`, `trace show/validate` |
| Root-owned task state | `task add/list/start/submit/accept/block/cancel` |
| Verification | `test-target add/list/link`, `verify run` |
| Audit judgments | `validation record`, `finding record`, `decision record` |
| Delivery decision | `gate record`, `delivery record` |
| Recovery | `migrate`, `repair`, `projection rebuild`; `doctor` validates invariants |

Events are compact append-only audit facts, not a replay source. Migration and administrator recovery use verified SQLite backups. There is no Connector, adapter, provider, dispatch, host receipt, checkpoint, or event export runtime.

## Root-Owned Task Lifecycle

Task state is single-writer:

```text
planned -> active -> submitted -> accepted
                    |           -> blocked
                    -> blocked
planned/active/submitted -> cancelled
```

Example:

```bash
harness.py --root . task add --id T1 --task "Implement profile CRUD" \
  --owner developer --acceptance AC1 --failure-mode FM1
harness.py --root . task start T1
harness.py --root . task submit T1 --context-id producer-context \
  --evidence "implementation returned to root controller"
harness.py --root . task accept T1 --evidence "independent review accepted"
```

There are no leases, heartbeat, fence, claim/release, stale recovery, review lease, retry budget, global request-id command log, or worker database writes. SQLite transactions, natural keys, and explicit state preconditions prevent duplicate mutation.

## Immutable Verification

Register an exact target, then let the root controller execute it:

```bash
harness.py --root . test-target add --id UNIT --kind unit \
  --command-template "python3 -m unittest" --result-format regex
harness.py --root . test-target link --task T1 --target UNIT
harness.py --root . verify run --target UNIT --acceptance AC1 --failure-mode FM1
```

`verify run` executes outside the write transaction, then atomically records one immutable current-candidate execution, validation, links, artifact digest, structured count/semantic result, runner, sandbox/no-network status, policy status, and compact audit event. A free-form `validation record` is judgment-only and cannot create gate-eligible execution evidence.

Container verification must really run with no network when the target requires it. Unavailable container capability fails closed; do not call it sandbox verification.

High or critical work without verifiable current-candidate execution provenance and distinct producer/reviewer context must return `human-review-required`, unless the user explicitly accepts every remaining risk with complete, current, unexpired metadata. Never fabricate Host, CI, HMAC, Connector, or receipt provenance.

## Quality Review And Delivery Handoff

Before a passing gate:

1. Confirm the candidate identity and worktree state under review.
2. Map delivered behavior to acceptance criteria and active failure modes.
3. Confirm exact tests/checks actually run on the current candidate; `skipped`, `blocked`, `not-run`, and fixture-only are not passes.
4. Resolve or explicitly record independent QA findings and residual risk.
5. Confirm immutable executions are current, structured, positive-count where required, artifact-consistent, and policy-compliant.
6. Confirm same-context review is labeled degraded and high/critical work follows `human-review-required` semantics.
7. Run adversarial review for logic gaps, false facts, simpler alternatives, data loss, stale candidate, forged evidence, and missing verification.

Record review and delivery only after those checks:

```bash
harness.py --root . gate record --reviewer-context fresh \
  --reviewer-context-id reviewer-context --result pass
harness.py --root . validate --delivery
harness.py --root . delivery record --scope "..." --acceptance AC1 \
  --changed-files "..." --validation "..." --qa "..." \
  --failure-mode-coverage "..." --quality-gate "pass" \
  --known-gaps "..." --handoff "..."
```

The final handoff must report:

- delivered behavior and acceptance mapping;
- changed files/modules and current candidate;
- exact tests/checks with counts and outcomes;
- independent QA and quality-gate result;
- failure-mode coverage or accepted/exempt risk metadata;
- migration/data/config implications;
- local artifact paths;
- known gaps, not-run checks, and residual risk;
- explicit statement that deployment is not included.

## Work Discipline

Before implementation, restate the root problem, split it into the smallest verifiable units, and explain why key decisions are made. Preserve unrelated user work.

Before handoff, challenge the result from four angles: logic gaps, incorrect facts, simpler alternatives, and verification evidence. Do not claim completion because the code merely looks correct.

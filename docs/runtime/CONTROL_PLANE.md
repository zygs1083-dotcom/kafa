# Local Delivery Control Plane

Codex Project Harness separates specification, host lifecycle, and delivery
trust so conversational convenience cannot bypass verified handoff. The
business-project runtime is local-only and uses schema 30.

## Authority and Layers

| Layer | Responsibility | Non-bypass boundary |
| --- | --- | --- |
| OpenSpec | Own proposal, design, behavioral specs, implementation tasks, and archive for substantial changes. | Kafa may reference OpenSpec paths and IDs, but it does not copy the spec into a competing source of truth. |
| Skill entry | Route work, explain responsibilities, and guide the root controller. | Instructions are not runtime facts and cannot create trusted evidence. |
| Plugin distribution | Package the approved Skills, Hooks, role templates, schemas, local runtime, and installer metadata. | Distribution metadata does not decide delivery readiness. |
| Native Codex/ChatGPT host | Own visible tasks, threads, subagents, worktrees, approval, model, cancellation, steering, and handoff. | Kafa does not implement a second lifecycle or treat self-reported host identifiers as trust anchors. |
| Hooks advisory layer | Read local status, inject role boundaries, and warn at Stop. | Hooks are warn-only, skip uninitialized projects, and never write delivery-eligible facts. |
| Local delivery runtime | Store current-cycle facts in SQLite, run controller verification, record review findings, and make the delivery decision. | This is the only layer that can determine Kafa handoff readiness. Generated Markdown is read-only projection output. |
| Release evaluation | Test the Kafa distribution, migrations, negative gates, and real native-host compatibility. | A Kafa release evaluation does not prove that an arbitrary project candidate passed its own acceptance criteria. |

## Responsibility Split

```text
OpenSpec change
  proposal + design + specs + tasks
                  |
                  v
Native Codex/ChatGPT host
  visible work + approvals + local candidate
                  |
                  v
Kafa root controller
  task intent -> exact target -> immutable execution
              -> validation link -> independent review
              -> candidate-scoped delivery decision
                  |
                  v
Local Git/content identity + SQLite + optional no-network container
```

The root controller is the sole writer of Kafa SQLite facts. Subagents and
reviewers return code, commands, findings, and context through the native host.
They do not claim database leases or advance their own task state.

## Non-Bypass Rules

- OpenSpec is the specification authority whenever the change is routed there;
  Kafa records only the delivery facts needed to verify the candidate.
- Skills, templates, and Hooks may guide behavior but cannot create immutable
  executions, passing validations, quality gates, or delivery records by
  themselves.
- The native host owns task/thread/subagent/worktree/approval/model/cancel and
  handoff lifecycle. Kafa neither spawns a hidden worker nor manufactures host
  provenance.
- Only root-controller `verify run` can create a delivery-eligible execution.
  Free-form validation text remains audit-only.
- Every passing execution must bind to the current cycle, current candidate,
  exact registered target, artifact digest, positive test count, semantic
  result, and actual sandbox/no-network policy status.
- A candidate change invalidates old execution and review credit for delivery.
  Historical rows remain auditable.
- A passing review gate cannot be recorded with a dirty Git worktree.
- High/critical work without verifiable provenance remains
  `human-review-required`. Distinct local context strings document procedural
  separation but are not cryptographic proof.
- A user-directed high-risk acceptance is valid only when actor, reason, scope,
  current revision, and unexpired expiry are recorded. It remains procedural.
- `skipped`, `blocked`, `not-run`, unavailable, and fixture-only outcomes are
  never reported as passes.

## Local Runtime Boundary

The supported runtime uses the project filesystem, local Git or content
identity, per-project SQLite, local commands, and an optional local no-network
container. It does not call remote project-management APIs, request integration
tokens, invoke `gh api`, or synchronize project state to an external service.

External apps remain a user/host capability. Their outputs do not enter Kafa's
active schema or satisfy delivery trust.

## Fact and Trust Flow

1. Read applicable project instructions and the authoritative OpenSpec change.
2. Confirm the real workspace, current branch/revision, and dirty state.
3. Initialize the local runtime and record acceptance-linked task intent.
4. Let the native host perform implementation in its visible task or worktree.
5. Return to the root workspace and establish the current candidate.
6. Run an exact registered target with root-controller `verify run`.
7. Store one immutable execution and link validation judgment to it.
8. Obtain an independent review through a distinct native-host context.
9. Resolve findings and apply the honest high-risk policy.
10. Record delivery only when the current-candidate decision has no blockers.

The database stores the execution once. Validation, review, and delivery logic
read that normalized fact instead of copying command output or accepting a
manual claim.

## Recovery Boundary

Compact append-only audit events explain local mutations, but they do not
contain whole-database snapshots and are not replay or recovery input.

Schema migration and administrator repair use verified SQLite backups with
integrity checks and digests. Schema 27/28/29 upgrades are built side by side;
schema 30 becomes active only after schema, foreign-key, invariant, and
projection checks. Removed remote and host-lifecycle history stays in the
pre-migration backup rather than entering the active database.

## Kernel Module Contracts

- `core.api` is the **explicit public API** consumed by the runtime CLI.
- **Schema Lifecycle** in `core/schema_lifecycle.py` owns initialization,
  side-by-side migration, verified backup, and rollback behavior.
- **Cycle Ledger** in `core/cycle_ledger.py` owns current-cycle identity,
  baseline validity, and traceability reads.
- `core/execution.py` owns local/container execution, target policy, result
  parsing, artifacts, and immutable execution metadata.
- **Delivery Decision** in `core/delivery.py` evaluates only current-cycle and
  current-candidate facts under the local trust policy.
- `core/projections.py` rebuilds only affected generated views during normal
  mutation; an administrator can request a full local rebuild.

The public CLI is restricted to initialization/status, cycle and requirement
facts, root-owned task state, test targets and verification, local judgments,
delivery decisions, migration/repair, and projection rebuild. Removed v1
families are not kept as empty compatibility layers.

## Packaged Surface

The Plugin exposes seven delivery-focused Skills, three role templates
(`developer`, `architect`, `qa-reviewer`), and exactly three Hooks
(`SessionStart`, `SubagentStart`, `Stop`).

`kafa project doctor --repo <project>` checks an ordinary project. `kafa doctor
--repo <kafa-source>` checks Kafa or Plugin source layout. Neither command
changes the ownership boundaries above.

## Why

Kafa's delivery safeguards are now stronger than its daily operator experience:
the same workflow policy is narrated in several files, simple patches require
manual graph wiring, default status output exposes too much scaffolding, and
structured facts are repeatedly rewritten as prose. This consumes operator and
model attention without increasing delivery trust.

The kernel should therefore collapse setup and derive presentation while
preserving every local-only, root-controller, immutable-execution,
current-candidate, and fail-closed boundary.

## What Changes

- Add one machine-readable canonical workflow policy for authority, workflow,
  safeguards, routes, obligations, and advanced-mode triggers. Overview,
  quickstart, Skill, trigger, and example content become generated or
  drift-checked views plus explicitly bounded exceptions.
- Add transactional `quickstart delivery-plan` setup that generates IDs and
  atomically creates the requirement, acceptance, optional failure-mode, task,
  target, qualification, and relation graph while leaving the task planned and
  scope unconfirmed. Baseline confirmation and verification remain explicit
  root-controller actions.
- Add a structured `verified-patch` result derived from the existing immutable
  execution and validation transaction. It always reports gate and delivery as
  not-run until those independent actions actually occur and adds no authority
  table or self-reported receipt.
- Make human-readable `status`, `doctor`, and `quickstart status` concise by
  default: current state, highest-priority blocker, and one primary next action.
  Preserve complete facts through `--verbose` and stable `--json` output.
- Derive routine delivery narrative from current structured facts. Keep human
  input for rationale, unresolved risk, exception notes, and the final handoff;
  retain compatibility for existing explicit narrative inputs.
- Unify ordinary business-project usage under `kafa project ...`, hide internal
  phase and advanced delegation details unless triggered, derive distribution
  inventories from one manifest, and move audit/retrospective/live-host
  compatibility out of the default patch path.
- Scope live-host release checks to affected surfaces, consolidate duplicated
  release evidence derivation, retain stable summaries instead of volatile
  proof bundles in the main review surface, and represent absent field metrics
  with one truthful sentinel.
- Keep schema 31 and its 30-table inventory unchanged. No migration, remote
  Connector, Host lifecycle, automatic gate, release, deploy, or user-level
  installation change is introduced.

## Capabilities

### New Capabilities

None. This change reduces interaction and maintenance cost inside the existing
local delivery kernel rather than adding a separate product capability.

### Modified Capabilities

- `local-delivery-kernel`: define a single workflow-policy authority,
  transactional plan setup, concise/complete output modes, derived narrative,
  advanced-mode triggers, scoped release evidence, and truthful compact outcome
  absence while preserving all delivery prerequisites.

## Impact

- Runtime and CLI: `plugins/codex-project-harness/scripts/harness.py`,
  `harness_db.py`, selected `core` orchestration/presentation modules, and
  `kafa/cli.py`.
- Contracts: a new workflow-policy source and distribution manifest, the
  canonical local-delivery-kernel spec, public JSON schemas without a schema
  generation change, retained Skills, and documentation projections.
- Tests and validation: transactional rollback/red tests, CLI golden output,
  policy/document drift checks, delivery-gate compatibility, structure and
  installation checks, fixture/stability E2E, benchmarks, and independent QA.
- Release/evidence: workflow routing and evidence summaries may change, but no
  tag, release, deployment, production migration, remote runtime call, or
  user-plugin replacement is authorized by this change.

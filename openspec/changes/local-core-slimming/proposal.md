## Why

Kafa was expanded to coordinate GitHub, Linear, Notion, Figma, Slack, a legacy
Host Codex SDK bridge, and distributed-style worker state. The product goal has
now changed to a local-only delivery workflow, so those subsystems add
maintenance cost, false trust assumptions, and user friction without serving
the intended runtime.

This change makes the implementation match the new product boundary: native
Codex owns agent lifecycle, while Kafa remains a small local Kernel for facts,
controller verification, independent review, and truthful delivery decisions.

## What Changes

- **BREAKING** Remove the five direct external connectors and all connector
  profile, namespace, retry, budget, outbox, recovery, fallback, adapter, and
  Apps/MCP receipt-governance runtime surfaces.
- **BREAKING** Remove connector-origin HMAC, CI verification, and external
  session verification as delivery trust anchors. Replace them with an honest
  local policy that blocks high-risk autonomous delivery unless the required
  local verification and explicit human-review conditions are present.
- **BREAKING** Remove the legacy Host Codex SDK provider, watchdog, model/Spark
  environment policy, fake SDK lifecycle, and CSV provider compatibility.
- **BREAKING** Collapse Kafa-owned dispatch orchestration to controller-side
  verification of the current local candidate. Native Codex remains the sole
  owner of subagents, worktrees, approvals, model routing, cancellation, and
  handoff.
- **BREAKING** Replace lease/fence-heavy task mutation with a root-controller
  single-writer lifecycle and reduce the public CLI accordingly.
- Normalize controller execution, evidence, tests, and validation so immutable
  execution facts are stored once and validations reference them.
- Keep audit events but remove whole-database mutation snapshots and public
  event-replay claims; use SQLite backups for recovery and schema migration.
- Reduce default Skills, Agent templates, Hooks, generated projections, schemas,
  tests, and documentation to the local delivery path.
- Preserve local Git/content identity, Delivery Cycles, requirements,
  acceptance, failure modes, structured test parsing, optional no-network
  container verification, findings, QA, delivery gates, installation, and
  release compatibility checks.

## Capabilities

### New Capabilities

- `local-delivery-kernel`: Defines the local-only product boundary, simplified
  runtime lifecycle, local verification and trust semantics, migration rules,
  and forbidden external/legacy behavior.

### Modified Capabilities

None. This repository has no previously archived OpenSpec capability specs;
the current behavior is documented in runtime ADRs and tests.

## Impact

- Runtime schema advances from 29 to a new breaking migration version.
- Public `connector`, `adapter`, provider, CSV, external verification, and
  distributed task-coordination CLI surfaces are removed or replaced.
- `harness_db.py`, `core/agent_provider.py`, `core/agent_runner.py`, connector
  trust code, schemas, projections, E2E scenarios, tests, Skills, Hooks,
  installer validation, README, Runtime documentation, and changelog are
  affected.
- Existing schema 29 databases require an automatic backup and local-fact
  migration. Removed external/provider records remain available only in the
  pre-migration backup and are never imported into the active local Kernel.
- Local Git and GitHub Actions used to maintain and release Kafa remain in
  scope. Business-project runtime makes no external connector calls.

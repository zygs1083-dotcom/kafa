# Architecture Control Plane

Codex Project Harness is an architecture control plane for verified code delivery. It keeps Skill, Plugin, Hooks, Host Bridge, Kernel, Connectors, and Evals in separate trust layers so conversational convenience cannot bypass delivery gates.

## Layers

| Layer | Responsibility | Boundary |
| --- | --- | --- |
| Skill Entry | Natural-language entrypoints that guide project work and point agents to runtime commands. | Skills are instructions, not facts. They do not create trusted evidence or change delivery status by themselves. |
| Plugin Distribution | Plugin manifest, packaged skills, hooks, templates, schemas, and local marketplace installation through `kafa`. | Plugin metadata distributes the control plane; it does not execute gates or mutate runtime state. |
| Hooks Advisory Layer | Codex lifecycle reminders for status, role boundaries, write warnings, change summaries, and Stop-time validation hints. | Hooks are advisory/warn-only by default and never write delivery-eligible evidence. |
| Host Bridge / Provider Layer | Native host receipt import and the optional legacy Host Codex bridge collect external agent reports. | Provider and host receipts are raw reports until controller `dispatch verify-attempt` creates trusted evidence. |
| Kernel Trust Layer | SQLite fact source, scheduler, leases, idempotency, controller verification, HMAC/session attestation, integration, and delivery gates. | This is the only layer allowed to decide delivery readiness. Markdown projections are generated views. |
| Connector / Eval Boundary | GitHub/Linear/Notion/Figma/Slack adapters synchronize workflow records; evals measure the control plane. | Connectors may write only to the current project's bound profile scopes. Connector outputs and eval results are audit or release signals, not delivery evidence for a project task. |

## Non-Bypass Rules

- Skill, Hooks, Host Bridge, Connectors, and Evals must not produce delivery-eligible evidence directly.
- Trusted evidence must bind to Kernel-controlled verification: parsed execution evidence, current code identity, target mapping, HMAC/session attestation where required, and integration/delivery gate checks.
- Delivery Cycles, Connector outbox recovery state, and Connector namespace profiles are Kernel facts. Skills and hooks may prompt `cycle start/status/close` or `connector profile status/set/unset`, but only Kernel schema 29 state determines which cycle-local fact, candidate, connector claim, bound external scope, or recovered external marker is delivery-relevant.
- Target sandbox policy, stack profile, and structured test semantic status are Kernel facts. Local runner output cannot satisfy targets that require sandbox or no-network, and regex output cannot impersonate structured result evidence.
- Host Codex and native fan-out worker output always enters as `agent_reports` plus `task_attempts(status=reported)` before controller verification.
- Native host model policy is authoritative. The legacy bridge may use an explicitly configured `spark-deterministic` compatibility model for low-risk, testable developer assignments, but selected model metadata never creates delivery evidence or relaxes Kernel verification.
- Connector writes are external workflow synchronization records governed by project profile scope checks plus transactional outbox claim/recovery. Harness never creates external workspaces, projects, channels, files, or repos; it only binds the project to existing targets. Connector links cannot satisfy high/critical delivery gates without existing connector(HMAC) trust anchors.
- Apps/MCP is the target authorization and external-action boundary. The Kernel accepts only receipts bound to the exact action, fence, payload hash, project key, and external scope; model-visible tool output is audit-only unless a non-forgeable host attestation is available. Current direct HTTP/`gh` adapters are `legacy-direct` compatibility paths. See [Apps/MCP Connector Receipt Boundary](APPS_MCP_RECEIPT_ADR.md).
- Native Codex/ChatGPT owns task, thread, subagent, worktree, approval, model, steer, cancel, handoff, and archive lifecycle. Kafa exports immutable constraints and imports host receipts with real IDs; it does not manufacture a parallel native lifecycle. Mutable SQLite facts remain single-writer in the root workspace. See [Native Codex Runtime Ownership](NATIVE_CODEX_RUNTIME_ADR.md).
- Evals are release gates for the harness itself. They do not prove an arbitrary project task is ready to deliver.

## Control Flow

```text
User intent
  -> Skill Entry
  -> Plugin-distributed runtime CLI
  -> Kernel task/requirement/scheduler state
  -> Current Delivery Cycle and candidate identity
  -> Host Bridge or local runner produces raw attempt
  -> Kernel controller verification produces trusted evidence
  -> Integration gate verifies branch identity and file claims
  -> Delivery gate checks evidence, QA, risk, and trust anchors
  -> Evals/doctor validate the harness release itself
```

`kafa doctor --repo .` includes a `control plane contract` check that verifies the packaged components still declare these boundaries.

## Kernel Module Contracts

- `core.api` is the **explicit public API** consumed by the runtime CLI. It lists supported symbols and does not dynamically re-export implementation details.
- **Schema Lifecycle** (`core/schema_lifecycle.py`) owns transactional DDL, compatibility columns, and schema initialization.
- **Cycle Ledger** (`core/cycle_ledger.py`) owns current-cycle identity, baseline validity, and traceability read models.
- **Delivery Decision** (`core/gate_engine.py`) consumes Cycle Ledger facts and trust-policy inputs without importing the monolithic compatibility module.

The feature freeze protects CLI, migration, trust, plugin, Hook/Skill, runtime-script, and release contracts. Internal core files may be split or renamed when those contracts and their regression evidence remain intact.

Native ChatGPT/Codex owns concrete model, reasoning, sandbox, approval, and task lifecycle selection. The legacy Host Codex Provider retains environment-variable compatibility only: `HARNESS_CODEX_MODEL` is the highest-priority hard override; without it, `spark-deterministic` requires an explicit `HARNESS_CODEX_SPARK_MODEL`. Kafa does not embed a preview model slug or treat model selection as evidence.

The base `kafa` installer and Kernel are stdlib-only. The legacy Host Codex SDK bridge is an explicit optional capability installed with `kafa[host-codex]`; missing SDK support fails closed at provider execution and does not affect plugin installation, doctor, project launchers, or non-Host delivery governance.

The legacy bridge cannot inherit a native parent task's permission and approval policy. It is disabled by default and runs only when the operator explicitly sets `HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all`, accepting the fixed worktree sandbox and deny-all approval mode. Native tasks remain authoritative and need no such compatibility opt-in.

When explicitly enabled, the legacy bridge remains fail-closed: a separate watchdog enforces its deadline, dead workers cannot receive heartbeat lease extensions, and DB/file CAS prevents late reports from creating evidence. It kills the known process tree, but cannot prove that an already reparented helper is gone; therefore cancel/timeout never auto-replans a legacy assignment and remains `verification_failed`. These controls do not make the bridge native.

Cold-start guidance is explicit. Use `kafa project doctor --repo <project>` for ordinary projects, and use `quickstart status` or `quickstart minimal --execute` inside the harness runtime when a new user needs a guided first loop. These commands call the Kernel runtime and existing dispatch evidence paths; they do not create delivery evidence outside controller execution, and they do not weaken delivery readiness.

`dispatch route-advice` gives the controlling model a read-only capability and risk report before native execution. It can identify small controller-verifiable candidates, but it cannot choose a model, spawn subagents, create evidence, or bypass the Kernel trust layer. With a run id it points to immutable `dispatch native-export` packages.

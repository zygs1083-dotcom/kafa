# Architecture Control Plane

Codex Project Harness is an architecture control plane for verified code delivery. It keeps Skill, Plugin, Hooks, Host Bridge, Kernel, Connectors, and Evals in separate trust layers so conversational convenience cannot bypass delivery gates.

## Layers

| Layer | Responsibility | Boundary |
| --- | --- | --- |
| Skill Entry | Natural-language entrypoints that guide project work and point agents to runtime commands. | Skills are instructions, not facts. They do not create trusted evidence or change delivery status by themselves. |
| Plugin Distribution | Plugin manifest, packaged skills, hooks, templates, schemas, and local marketplace installation through `kafa`. | Plugin metadata distributes the control plane; it does not execute gates or mutate runtime state. |
| Hooks Advisory Layer | Codex lifecycle reminders for status, role boundaries, write warnings, change summaries, and Stop-time validation hints. | Hooks are advisory/warn-only by default and never write delivery-eligible evidence. |
| Host Bridge / Provider Layer | Host Codex, manual CSV, and fixture providers create or collect external agent reports. | Provider reports are raw reports until controller `dispatch verify-attempt` creates trusted evidence. |
| Kernel Trust Layer | SQLite fact source, scheduler, leases, idempotency, controller verification, HMAC/session attestation, integration, and delivery gates. | This is the only layer allowed to decide delivery readiness. Markdown projections are generated views. |
| Connector / Eval Boundary | GitHub/Linear/Notion/Figma/Slack adapters synchronize workflow records; evals measure the control plane. | Connectors may write only to the current project's bound profile scopes. Connector outputs and eval results are audit or release signals, not delivery evidence for a project task. |

## Non-Bypass Rules

- Skill, Hooks, Host Bridge, Connectors, and Evals must not produce delivery-eligible evidence directly.
- Trusted evidence must bind to Kernel-controlled verification: parsed execution evidence, current code identity, target mapping, HMAC/session attestation where required, and integration/delivery gate checks.
- Delivery Cycles, Connector outbox recovery state, and Connector namespace profiles are Kernel facts. Skills and hooks may prompt `cycle start/status/close` or `connector profile status/set/unset`, but only Kernel schema 28 state determines which cycle, candidate, connector claim, bound external scope, or recovered external marker is delivery-relevant.
- Target sandbox policy, stack profile, and structured test semantic status are Kernel facts. Local runner output cannot satisfy targets that require sandbox or no-network, and regex output cannot impersonate structured result evidence.
- Host Codex and native fan-out worker output always enters as `agent_reports` plus `task_attempts(status=reported)` before controller verification.
- Host Codex model policy is execution routing only. `spark-deterministic` may select a faster SDK model for low-risk, testable developer assignments, but selected model metadata never creates delivery evidence or relaxes Kernel verification.
- Connector writes are external workflow synchronization records governed by project profile scope checks plus transactional outbox claim/recovery. Harness never creates external workspaces, projects, channels, files, or repos; it only binds the project to existing targets. Connector links cannot satisfy high/critical delivery gates without existing connector(HMAC) trust anchors.
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

From v1.23.0, Host Codex model policy is opt-in through environment variables. Keep `HARNESS_CODEX_MODEL_POLICY=default` for SDK defaults, or use `spark-deterministic` only when controller-verifiable low-risk developer tasks are acceptable Spark candidates. `HARNESS_CODEX_MODEL` remains a hard override. If Spark is unavailable, the provider fails closed through the normal Host Codex lifecycle; it does not silently promote or demote model capability.

From v1.24.0, cold-start guidance is explicit. Use `kafa project doctor --repo <project>` for ordinary projects, and use `quickstart status` or `quickstart minimal --execute` inside the harness runtime when a new user needs a guided first loop. These commands call the Kernel runtime and existing dispatch evidence paths; they do not create delivery evidence outside controller execution, and they do not weaken delivery readiness.

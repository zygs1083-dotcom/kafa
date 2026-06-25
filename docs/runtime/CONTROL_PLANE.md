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
| Connector / Eval Boundary | GitHub/Linear/Notion/Figma/Slack adapters synchronize workflow records; evals measure the control plane. | Connector outputs and eval results are audit or release signals, not delivery evidence for a project task. |

## Non-Bypass Rules

- Skill, Hooks, Host Bridge, Connectors, and Evals must not produce delivery-eligible evidence directly.
- Trusted evidence must bind to Kernel-controlled verification: parsed execution evidence, current code identity, target mapping, HMAC/session attestation where required, and integration/delivery gate checks.
- Host Codex and native fan-out worker output always enters as `agent_reports` plus `task_attempts(status=reported)` before controller verification.
- Connector writes are external workflow synchronization records. They can attach links but cannot satisfy high/critical delivery gates without existing connector(HMAC) trust anchors.
- Evals are release gates for the harness itself. They do not prove an arbitrary project task is ready to deliver.

## Control Flow

```text
User intent
  -> Skill Entry
  -> Plugin-distributed runtime CLI
  -> Kernel task/requirement/scheduler state
  -> Host Bridge or local runner produces raw attempt
  -> Kernel controller verification produces trusted evidence
  -> Integration gate verifies branch identity and file claims
  -> Delivery gate checks evidence, QA, risk, and trust anchors
  -> Evals/doctor validate the harness release itself
```

`kafa doctor --repo .` includes a `control plane contract` check that verifies the packaged components still declare these boundaries.

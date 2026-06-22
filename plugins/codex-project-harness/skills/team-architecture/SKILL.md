---
name: "team-architecture"
description: "Use when a code-delivery project needs the right Codex agent team, project-specific skills, role sessions, or subagent routing selected from Pipeline, Fan-out/Fan-in, Expert Pool, Producer-Reviewer, Supervisor, and limited Hierarchical Delegation. Use for planning implementation and QA teams, not deployment teams."
---

# Team Architecture

Design the smallest effective team for the project.

## Architecture Patterns

| Pattern | Use When |
| --- | --- |
| Supervisor | A central project manager must route tasks and maintain state |
| Producer-Reviewer | Work needs independent review before acceptance |
| Pipeline | Requirements, design, development, testing, and delivery handoff are sequential |
| Fan-out/Fan-in | Multiple independent perspectives should be explored in parallel |
| Expert Pool | Specialists are needed only for certain risks |
| Limited Hierarchical Delegation | A large project has multiple modules and 20+ meaningful tasks |

Default to `Supervisor + Producer-Reviewer`. Add other patterns only when justified.

## Delivery-Only Boundary

Design teams for requirements, architecture, implementation, QA, security review, documentation, and delivery handoff. Do not create deployment, production release, cloud provisioning, secret-rotation, or paid-resource agents inside this harness.

## Collaboration Tool Ownership

Assign tool ownership only when it helps delivery:

| Tool | Typical owner | Use |
| --- | --- | --- |
| Git / GitHub | Developer / QA Reviewer / Delivery Coordinator | Branches, commits, PRs, checks, reviews, issue links |
| Linear | Project Manager / Developer / QA Reviewer | Task breakdown, status, acceptance mapping |
| Notion | Product Analyst / Architect / Delivery Coordinator | PRD, decisions, architecture notes, delivery record |
| Figma | Product Analyst / Architect / QA Reviewer | Design context, component references, visual acceptance |
| Slack | Project Manager / Delivery Coordinator | Status updates and review/delivery notices when useful |

Codex decides which tools are useful from context. Ask only before high-impact external actions.

## Session Levels

- Project Manager: the controlling conversation and state owner.
- Domain Sessions: role-based contexts such as Product, Architect, Developer, QA, Security, and Delivery Coordinator. Use real separate sessions only when available; otherwise emulate them with labeled sections.
- Subagents: short-lived task workers inside a domain session. They may split tests or reviews by surface area, such as QA-A API contract, QA-B UI flow, QA-C data/schema safety. Require concrete evidence from each subagent.

## Agent Creation Rule

Create or recommend a custom agent only when the role:

- repeats across multiple phases,
- has a stable and narrow responsibility,
- needs distinct tools, permissions, model choice, or review posture,
- differs meaningfully from a generic worker/explorer.

## Skill Creation Rule

Create a project-specific skill only when the workflow:

- will repeat,
- has stable inputs and outputs,
- is independently triggerable,
- is testable,
- avoids duplicating an existing skill.

## Output

```text
# Team Architecture

## Selected Patterns
## Delivery Boundary
## Agents
## Skills
## Message Protocol
## Task Routing
## Tool Ownership
## Review Gates
## Escalation Rules
```

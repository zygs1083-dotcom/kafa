---
name: "team-architecture"
description: "Use when a project needs the right Codex agent team, project-specific skills, or a harness-style team architecture selected from Pipeline, Fan-out/Fan-in, Expert Pool, Producer-Reviewer, Supervisor, and limited Hierarchical Delegation."
---

# Team Architecture

Design the smallest effective team for the project.

## Architecture Patterns

| Pattern | Use When |
| --- | --- |
| Supervisor | A central project manager must route tasks and maintain state |
| Producer-Reviewer | Work needs independent review before acceptance |
| Pipeline | Requirements, design, development, testing, and release are sequential |
| Fan-out/Fan-in | Multiple independent perspectives should be explored in parallel |
| Expert Pool | Specialists are needed only for certain risks |
| Limited Hierarchical Delegation | A large project has multiple modules and 20+ meaningful tasks |

Default to `Supervisor + Producer-Reviewer`. Add other patterns only when justified.

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
## Agents
## Skills
## Message Protocol
## Task Routing
## Review Gates
## Escalation Rules
```

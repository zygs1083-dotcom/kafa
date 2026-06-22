---
name: "harness-audit"
description: "Use when auditing or repairing drift in a Codex project harness, including AGENTS.md, .ai-team, .agents/skills, .codex/agents, docs/harness, workflows, and task state."
---

# Harness Audit

Check whether the project operating system still matches reality.

## Inspect

- `AGENTS.md`
- `.ai-team/`
- `.agents/skills/`
- `.codex/agents/`
- `docs/harness/`
- task board and decision log
- acceptance criteria and traceability
- recent code changes and tests

## Detect

- duplicate or conflicting agents,
- stale requirements,
- missing owners,
- unclear review gates,
- skills that no longer trigger correctly,
- task state that disagrees with code reality,
- missing escalation points,
- uncontrolled growth of logs or generated artifacts.

## Output

```text
# Harness Audit

## Healthy
## Drift
## Required Repairs
## Optional Improvements
## Risks
```

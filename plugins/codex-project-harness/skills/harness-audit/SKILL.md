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
- current candidate, local runtime records, and evidence
- schema 31 execution provenance: target definition, controller runtime digest,
  policy version, optional frozen local container endpoint/image digest, and
  `provenance_status`
- recent code changes and local tests
- `kafa project status --repo . --verbose`
- `kafa project validate --repo . --delivery`

## Detect

- duplicate or conflicting agents,
- stale requirements,
- missing owners,
- unclear review gates,
- skills that no longer trigger correctly,
- task state that disagrees with code reality,
- runtime records that disagree with the current candidate or evidence,
- complete provenance with missing `target_definition_sha256` or
  `runtime_executable_sha256`, and `legacy-incomplete` history incorrectly used
  for current delivery,
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

<!-- BEGIN GENERATED: workflow-contract:harness-audit-trigger -->
## Trigger (Non-Default)

Trigger when: multi-day work, repeated escapes, schema or runtime change or drift, or milestone review

Activates: harness-audit

This Skill is not part of the default small single-producer path. Once triggered, its complete evidence obligations remain active. If a required check is blocked, skipped, not-run, or unavailable, report that exact state; a fixture cannot substitute for required live evidence.
<!-- END GENERATED: workflow-contract:harness-audit-trigger -->

---
name: "project-retrospective"
description: "Use after a delivery milestone, bug fix, failed code delivery, or completed project implementation to extract lessons, update the harness, and decide whether any agent, skill, test, or workflow should change. Focus on code delivery, not production release."
---

# Project Retrospective

Turn project evidence into better future execution.

## Inputs

Review:

- completed tasks,
- failed or delayed tasks,
- bugs and regressions,
- review findings,
- user feedback,
- delivery issues,
- local workflow friction or useful automation,
- decisions and assumptions,
- useful prompts or workflows.

## Questions

- What worked and should be reused?
- What caused rework?
- Which checks caught real issues?
- Which checks were noisy?
- Which skill or agent should be added, edited, merged, or removed?
- Which local workflow step helped or created noise?
- What should be measured next time?

## Output

```text
# Retrospective

## Summary
## Wins
## Problems
## Root Causes
## Process Changes
## Skill / Agent Changes
## Runtime Changes
## Follow-Up Tasks
```

<!-- BEGIN GENERATED: workflow-contract:project-retrospective-trigger -->
## Trigger (Non-Default)

Trigger when: delivery milestone completes or a failure loop exposes a stable lesson

Activates: project-retrospective

This Skill is not part of the default small single-producer path. Once triggered, its complete evidence obligations remain active. If a required check is blocked, skipped, not-run, or unavailable, report that exact state; a fixture cannot substitute for required live evidence.
<!-- END GENERATED: workflow-contract:project-retrospective-trigger -->

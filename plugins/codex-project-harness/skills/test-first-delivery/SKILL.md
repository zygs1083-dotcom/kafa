---
name: "test-first-delivery"
description: "Use when the user asks to implement with tests first, add regression coverage, validate contracts, or deliver a feature through test-backed development."
---

# Test-First Delivery

Prefer evidence before implementation confidence.

## Workflow

1. Map the requirement to acceptance criteria.
2. Identify the contract: API shape, data schema, UI behavior, command output, or integration boundary.
3. Link the contract to GitHub/Linear issue IDs, Notion PRD sections, or Figma acceptance references when useful.
4. Add a failing test or executable check when practical.
5. Implement the smallest code needed to pass.
6. Add edge-case and regression checks proportional to risk.
7. Run relevant tests and inspect failures.
8. Record test evidence in local validation docs and external trackers when useful.
9. Ensure the final test proves behavior, not just existence.

## Completion Evidence

Report:

- test added or updated,
- command run,
- result,
- behavior covered,
- GitHub/Linear/Notion/Figma links or local fallback artifact,
- known gaps.

---
name: "test-first-delivery"
description: "Use when the user asks to implement with tests first, add regression coverage, validate contracts, or deliver a feature through test-backed development."
---

# Test-First Delivery

Prefer evidence before implementation confidence.

## Workflow

1. Map the requirement to acceptance criteria.
2. Identify the contract: API shape, data schema, UI behavior, command output, or integration boundary.
3. Add a failing test or executable check when practical.
4. Implement the smallest code needed to pass.
5. Add edge-case and regression checks proportional to risk.
6. Run relevant tests and inspect failures.
7. Ensure the final test proves behavior, not just existence.

## Completion Evidence

Report:

- test added or updated,
- command run,
- result,
- behavior covered,
- known gaps.

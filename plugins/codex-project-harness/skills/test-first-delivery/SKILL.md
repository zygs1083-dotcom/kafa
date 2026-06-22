---
name: "test-first-delivery"
description: "Use when the user asks to implement with tests first, add regression coverage, validate contracts, or deliver a feature through test-backed development. Trigger for 先写测试, 测试驱动, 补回归测试, 验证契约, test first, TDD, regression coverage, contract validation."
---

# Test-First Delivery

Prefer evidence before implementation confidence.

## Workflow

1. Map the requirement to acceptance criteria.
2. Identify the contract: API shape, data schema, UI behavior, command output, or integration boundary.
3. Link the contract to acceptance IDs and failure mode IDs when present.
4. Link the contract to GitHub/Linear issue IDs, Notion PRD sections, or Figma acceptance references when useful.
5. Add a failing test or executable check when practical.
6. Implement the smallest code needed to pass.
7. Add edge-case and regression checks proportional to risk.
8. Run relevant tests and inspect failures.
9. Record test evidence with `scripts/harness.py --root . evidence record ...`, `test record ...`, and `validation record ...`; mirror to external trackers when useful.
10. Ensure the final test proves behavior, not just existence.

## Test Exception Rule

If no automated or executable test is practical, record a reason code and alternate verification:

```text
Reason code: docs-only | exploratory | external-system-unavailable | legacy-no-test-hook | time-boxed-risk-accepted
Alternate verification:
Risk owner:
```

## Completion Evidence

Report:

- test added or updated,
- command run,
- result,
- behavior covered,
- evidence ID and validation record,
- GitHub/Linear/Notion/Figma links or local fallback artifact,
- failure modes covered or exemption reason,
- known gaps.

---
name: "delivery-readiness"
description: "Use when preparing a code delivery handoff after implementation and QA. Trigger for 交付检查, 验收交付, 交付就绪, 交付证据, handoff, delivery readiness, delivery evidence. Confirms scope, acceptance criteria, changed files, tests, independent review, known gaps, and handoff notes. This skill does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation."
---

# Delivery Readiness

Prepare evidence for verified code delivery.

## Checklist

1. Confirm the delivery scope and requirement baseline.
2. Map delivered behavior to acceptance criteria.
3. Confirm tests, type checks, lint checks, builds, or manual checks that were run.
4. Confirm independent QA findings are resolved or explicitly accepted as residual risk.
5. Identify changed files, contracts, data structures, configuration, and documentation.
6. Note local migration or data-shape implications when code changes include them.
7. Summarize Git/GitHub branch, PR, issue, check, and review evidence when available.
8. Confirm failure modes are covered, accepted, or explicitly exempt.
9. Confirm quality gate result, reviewed commit/revision, and reviewer context.
10. Summarize Linear issue status, Notion delivery notes, Figma design status, and Slack handoff status when useful.
11. List known gaps, skipped checks, and follow-up tasks.
12. State clearly that deployment and production release are outside this handoff.
13. Record delivery evidence with `scripts/record_delivery.py`.
14. Run `scripts/validate_harness_state.py` before claiming the handoff is ready.

## Output

```text
# Delivery Readiness

## Scope
## Acceptance Mapping
## Changed Files
## Validation
## Independent QA
## Collaboration Links
## Failure Mode Coverage
## Quality Gate
## Data / Config Notes
## Known Gaps
## Handoff Notes
## Out Of Scope
```

## Rule

Do not deploy, release to production, provision infrastructure, run production migrations, change secrets, or create paid resources. If the user requests those actions, end with a code delivery handoff and explain that deployment requires a separate workflow.

---
name: "delivery-readiness"
description: "Use when preparing a code delivery handoff after implementation and QA. Confirms scope, acceptance criteria, changed files, tests, independent review, known gaps, and handoff notes. This skill does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation."
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
8. Summarize Linear issue status, Notion delivery notes, Figma design status, and Slack handoff status when useful.
9. List known gaps, skipped checks, and follow-up tasks.
10. State clearly that deployment and production release are outside this handoff.

## Output

```text
# Delivery Readiness

## Scope
## Acceptance Mapping
## Changed Files
## Validation
## Independent QA
## Collaboration Links
## Data / Config Notes
## Known Gaps
## Handoff Notes
## Out Of Scope
```

## Rule

Do not deploy, release to production, provision infrastructure, run production migrations, change secrets, or create paid resources. If the user requests those actions, end with a code delivery handoff and explain that deployment requires a separate workflow.

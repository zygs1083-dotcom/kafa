---
name: "independent-quality-gate"
description: "Use when the user asks for independent QA, code review, acceptance review, system consistency checks, or validation before code delivery or merge. Reviews implementation, tests, and delivery evidence; does not approve deployment or production release."
---

# Independent Quality Gate

Review as an independent evaluator. Prioritize bugs, regressions, missing tests, security issues, and integration mismatches.

## Review Focus

Check:

- requirement to implementation traceability,
- Linear/GitHub/Notion/Figma mappings against local acceptance criteria when present,
- GitHub PR diff, checks, and review context when available,
- tests proving meaningful behavior,
- API response shape versus frontend types,
- database fields versus DTOs and validation,
- route definitions versus navigation,
- permission rules across frontend and backend,
- error codes versus UI error handling,
- local migration, schema-change, and reversal safety when code changes include data structures,
- observability for important failures.

## Subagent QA Split

For broad changes, split QA into short-lived subagents by risk surface. Examples:

- QA-A: API contracts, request/response shape, error handling.
- QA-B: UI behavior, navigation, loading and empty states.
- QA-C: data model, migrations, validation, idempotency, failure modes.
- QA-D: security, permissions, secrets exposure, dependency risk.

Each subagent must return evidence: files inspected, commands run, findings, and residual risk.

Use this output shape for each QA subagent:

```text
Surface:
Acceptance Criteria Checked:
Tool Context:
Files Inspected:
Commands Run:
Findings:
Severity:
Pass/Fail:
Residual Risk:
```

## Producer Separation

The reviewer should not rubber-stamp their own implementation. If you produced the change, switch into a stricter review stance and say so.

## Output

Lead with findings ordered by severity. If no issues are found, say that clearly and mention remaining test gaps or residual risk.

Include GitHub/Linear/Notion/Figma/Slack links or local fallback artifacts used during review.

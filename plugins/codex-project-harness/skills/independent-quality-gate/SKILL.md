---
name: "independent-quality-gate"
description: "Use when the user asks for independent QA, code review, acceptance review, system consistency checks, or validation before code delivery or merge. Trigger for 独立验收, 代码审查, QA, 质量门, 合并前检查, 交付前检查, independent review, quality gate. Reviews implementation, tests, and delivery evidence; does not approve deployment or production release."
---

# Independent Quality Gate

Review as an independent evaluator. Prioritize bugs, regressions, missing tests, security issues, and integration mismatches.

Prefer fresh-context review when possible. If the same conversation produced the implementation, mark the review as `same-context-degraded` and apply a stricter posture.

## Review Focus

Check:

- requirement to implementation traceability,
- failure mode coverage for risky behavior,
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
Record each material QA result with `scripts/harness.py --root . validation record ...`.
Record the gate decision with `scripts/harness.py --root . gate record ...`, including the reviewed commit or revision.

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

## Blocking Rules

- Critical or high findings fail the gate.
- Missing required validation fails or blocks the gate.
- Medium findings require explicit residual-risk acceptance.
- Same-context review can pass only with `reviewer_context: same-context-degraded` and clear residual-risk notes.
- Code changes after QA require a new gate record for the new commit or revision.

## Output

Lead with findings ordered by severity. If no issues are found, say that clearly and mention remaining test gaps or residual risk.

Include GitHub/Linear/Notion/Figma/Slack links or local fallback artifacts used during review.

Before passing delivery readiness, run `scripts/harness.py --root . validate --delivery` and report any warnings or errors.

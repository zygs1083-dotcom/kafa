---
name: "independent-quality-gate"
description: "Use when the user asks for independent QA, code review, acceptance review, system consistency checks, or validation before merge/release."
---

# Independent Quality Gate

Review as an independent evaluator. Prioritize bugs, regressions, missing tests, security issues, and integration mismatches.

## Review Focus

Check:

- requirement to implementation traceability,
- tests proving meaningful behavior,
- API response shape versus frontend types,
- database fields versus DTOs and validation,
- route definitions versus navigation,
- permission rules across frontend and backend,
- error codes versus UI error handling,
- migration and rollback safety,
- observability for important failures.

## Producer Separation

The reviewer should not rubber-stamp their own implementation. If you produced the change, switch into a stricter review stance and say so.

## Output

Lead with findings ordered by severity. If no issues are found, say that clearly and mention remaining test gaps or residual risk.

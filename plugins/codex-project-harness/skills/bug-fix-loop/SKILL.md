---
name: "bug-fix-loop"
description: "Use when fixing a bug, investigating an error, reproducing a failure, or preventing a regression from returning. Trigger for 修 bug, 排查错误, 复现问题, 失败定位, 防回归, fix bug, investigate error, reproduce failure, regression prevention."
---

# Bug Fix Loop

Fix the root cause, not just the symptom.

## Workflow

1. Capture the reported behavior and expected behavior.
2. Inspect related local issues, logs, specifications, and recorded delivery evidence when useful.
3. Reproduce the issue or explain why reproduction is blocked.
4. Locate the smallest failing path.
5. Add a regression test or executable check when practical.
6. Identify the root cause.
7. Apply a focused fix.
8. Re-run the reproduction and relevant tests.
9. Check nearby integration boundaries for related breakage.
10. Update the local task or specification status when useful.

## Output

```text
Root cause:
Fix:
Regression coverage:
Verification:
Evidence paths:
Residual risk:
```

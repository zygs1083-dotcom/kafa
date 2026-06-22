---
name: "bug-fix-loop"
description: "Use when fixing a bug, investigating an error, reproducing a failure, or preventing a regression from returning."
---

# Bug Fix Loop

Fix the root cause, not just the symptom.

## Workflow

1. Capture the reported behavior and expected behavior.
2. Reproduce the issue or explain why reproduction is blocked.
3. Locate the smallest failing path.
4. Add a regression test or executable check when practical.
5. Identify the root cause.
6. Apply a focused fix.
7. Re-run the reproduction and relevant tests.
8. Check nearby integration boundaries for related breakage.

## Output

```text
Root cause:
Fix:
Regression coverage:
Verification:
Residual risk:
```

---
name: "bug-fix-loop"
description: "Use when fixing a bug, investigating an error, reproducing a failure, or preventing a regression from returning."
---

# Bug Fix Loop

Fix the root cause, not just the symptom.

## Workflow

1. Capture the reported behavior and expected behavior.
2. Inspect related GitHub/Linear issues, PRs, logs, Notion notes, or Slack-reported context when useful.
3. Reproduce the issue or explain why reproduction is blocked.
4. Locate the smallest failing path.
5. Add a regression test or executable check when practical.
6. Identify the root cause.
7. Apply a focused fix.
8. Re-run the reproduction and relevant tests.
9. Check nearby integration boundaries for related breakage.
10. Update local or external task status when useful.

## Output

```text
Root cause:
Fix:
Regression coverage:
Verification:
Tool links:
Residual risk:
```

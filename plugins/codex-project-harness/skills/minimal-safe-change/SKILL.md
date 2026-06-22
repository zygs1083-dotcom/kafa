---
name: "minimal-safe-change"
description: "Use when the user asks for the smallest complete safe code change, wants to avoid unrelated refactors, or needs a focused patch with verification and reversal awareness. Trigger for Chinese or English requests such as 最小改动, 不做无关重构, 字段兼容, 局部修复, 小范围修改, small focused patch, compatibility fix, narrow safe change. Use for code delivery only; do not deploy or perform production operations."
---

# Minimal Safe Change

Make the smallest complete change that satisfies the requested behavior.

## Workflow

1. Inspect the current state and identify files likely in scope.
2. Inspect git branch/diff and any relevant GitHub/Linear issue or PR context when available.
3. Define the change contract: behavior, touched surface, tests, tool links, and non-goals.
4. Read before editing.
5. Modify only the necessary files.
6. Preserve existing style and local abstractions.
7. Add or update focused tests when risk justifies it.
8. Run the narrowest meaningful verification first, then broader checks if needed.
9. Inspect the diff for unrelated churn.
10. Update local task-board or external issue status when useful.
11. Report what changed, how it was verified, tool links used, and any residual risk.

## Guardrails

- Do not perform opportunistic refactors.
- Do not reformat unrelated files.
- Do not silently overwrite user changes.
- A small patch must still be complete enough to be safe.

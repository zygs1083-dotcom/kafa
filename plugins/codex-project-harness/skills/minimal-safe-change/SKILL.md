---
name: "minimal-safe-change"
description: "Use when the user asks for the smallest complete safe code change, wants to avoid unrelated refactors, or needs a focused patch with verification and reversal awareness. Trigger for Chinese or English requests such as 最小改动, 不做无关重构, 字段兼容, 局部修复, 小范围修改, small focused patch, compatibility fix, narrow safe change. Use for code delivery only; do not deploy or perform production operations."
---

# Minimal Safe Change

Make the smallest complete change that satisfies the requested behavior.

## Workflow

1. Inspect the current state and identify files likely in scope.
2. Inspect the local git branch/diff and relevant spec, task, or decision-log context when available.
3. Define the change contract: behavior, touched surface, tests, and non-goals.
4. Read before editing.
5. Modify only the necessary files.
6. Preserve existing style and local abstractions.
7. Add or update focused tests when risk justifies it.
8. Run the narrowest meaningful verification first, then broader checks if needed.
9. Run a quick adversarial review against logic gaps, incorrect facts, simpler alternatives, and verification evidence.
10. Inspect the diff for unrelated churn.
11. Update local task state when useful.
12. Report what changed, why key decisions were made, how it was verified, and any residual risk.

## Guardrails

- Do not perform opportunistic refactors.
- Do not reformat unrelated files.
- Do not silently overwrite user changes.
- A small patch must still be complete enough to be safe.
- Do not claim "looks good" without verification evidence or explicit residual risk.

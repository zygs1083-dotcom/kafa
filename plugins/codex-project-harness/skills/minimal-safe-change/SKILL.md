---
name: "minimal-safe-change"
description: "Use when the user asks for the smallest complete safe change, wants to avoid unrelated refactors, or needs a focused patch with verification and rollback awareness."
---

# Minimal Safe Change

Make the smallest complete change that satisfies the requested behavior.

## Workflow

1. Inspect the current state and identify files likely in scope.
2. Define the change contract: behavior, touched surface, tests, and non-goals.
3. Read before editing.
4. Modify only the necessary files.
5. Preserve existing style and local abstractions.
6. Add or update focused tests when risk justifies it.
7. Run the narrowest meaningful verification first, then broader checks if needed.
8. Inspect the diff for unrelated churn.
9. Report what changed, how it was verified, and any residual risk.

## Guardrails

- Do not perform opportunistic refactors.
- Do not reformat unrelated files.
- Do not silently overwrite user changes.
- A small patch must still be complete enough to be safe.

# Project Agent Instructions

This project uses Codex Project Harness.

## Operating Rules

- Keep changes scoped to the current task.
- Preserve user changes and inspect before editing.
- Before implementation, restate the root problem this task is meant to solve.
- Split work into the smallest verifiable units and complete them one by one.
- Explain the reason behind key decisions, not only the implementation steps.
- Before handoff, run an adversarial review against logic gaps, incorrect facts, simpler alternatives, and verification evidence.
- Do not claim "looks good" without verification evidence or explicit residual risk.
- Only the root controller writes Kafa SQLite facts and uses Kafa v2 runtime commands to record task, validation, decision, and delivery facts.
- Maintain `.ai-team/` control files for substantial work.
- Keep Kafa runtime local-only: use the project filesystem, local Git or content identity, per-project SQLite, and optional local container execution.
- Treat Native Codex/ChatGPT as the sole owner of task, thread, subagent, worktree, approval, model, cancellation, steering, and handoff lifecycle.
- Subagents return code or review evidence through the Native Host without mutating Kafa state.
- Independently verify the current local candidate before recording delivery facts; never substitute self-reported or fabricated evidence.
- Require `human-review-required` for high or critical risk without verifiable provenance unless the user explicitly accepts and records the complete risk.
- Separate producer and reviewer roles.
- Stop at verified code handoff; deployment and production operations are outside this harness.
- Report verification evidence with every completed task.

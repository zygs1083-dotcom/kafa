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
- Maintain `.ai-team/` control files for substantial work.
- Use project runtime scripts to update phase, task, validation, decision, and delivery records.
- Let Codex decide whether GitHub, Linear, Notion, Figma, or Slack is useful from context; use local harness files as fallback.
- Ask before high-impact external actions such as Slack messages, public/shared artifact creation, permission or secret changes, paid resources, destructive edits, or production-related changes.
- Separate producer and reviewer roles.
- Stop at verified code handoff; deployment and production operations are outside this harness.
- Report verification evidence with every completed task.

# ADR: Native Codex Runtime Ownership

- Status: Superseded by OpenSpec change `local-core-slimming`
- Original date: 2026-07-10
- Superseded: 2026-07-11

## Historical context

This ADR first established that Native Codex/ChatGPT, rather than Kafa, owns
task, subagent, worktree, approval, model, cancellation, steering, and handoff
lifecycle. Its transitional task-package, provider, dispatch, import, and Host
receipt design has since been removed.

## Superseding decision

The native Host remains the sole lifecycle owner. Kafa is only a local
verified-delivery Kernel: the root controller records local delivery facts,
runs current-candidate verification, records review judgments, and evaluates
the delivery gate. Workers and reviewers return code, commands, findings, and
risks through the Host and never write Kafa state themselves.

The authoritative decision and scenarios are:

- `openspec/changes/local-core-slimming/design.md`
- `openspec/changes/local-core-slimming/specs/local-delivery-kernel/spec.md`
- `docs/runtime/CONTROL_PLANE.md`

This short record is retained only to explain why older audits link to this
filename. It is not an active provider, receipt, or dispatch contract.

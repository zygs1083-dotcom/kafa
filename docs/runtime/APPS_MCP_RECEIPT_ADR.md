# ADR: Apps/MCP Connector Receipt Boundary

- Status: Superseded by OpenSpec change `local-core-slimming`
- Original date: 2026-07-10
- Superseded: 2026-07-11

## Historical context

This ADR explored keeping direct external Connectors until a Host-verifiable
Apps/MCP result receipt existed. That transitional design is no longer an
active Kafa contract.

## Superseding decision

Kafa v2 has a local-only business-project runtime. It contains no direct
GitHub, Linear, Notion, Figma, or Slack client; no Connector token/profile;
no outbox/adapter lifecycle; and no synthetic Host receipt path. Native
Codex/ChatGPT owns external tools and their approvals outside the Kafa Kernel.
External tool output cannot become delivery provenance through Kafa.

The authoritative decision and scenarios are:

- `openspec/changes/archive/2026-07-15-local-core-slimming/design.md`
- `openspec/specs/local-delivery-kernel/spec.md`
- `docs/runtime/CONTROL_PLANE.md`

This short record is retained only to explain why older audits link to this
filename. None of the removed receipt, adapter, or direct-Connector design is
supported runtime guidance.

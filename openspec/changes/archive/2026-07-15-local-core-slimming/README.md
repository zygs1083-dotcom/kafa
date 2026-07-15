# local-core-slimming

Slim Kafa into a local-only verified delivery kernel

## Status

- OpenSpec workflow: `spec-driven`
- Artifacts: complete and validated
- Implementation: not started
- Target branch: `v2-local-core-slimming`
- Target release: `2.0.0-beta.1 / Runtime 5.0.0 / schema 30`

## Reading Order

1. `proposal.md`: why the product boundary changes and what breaks.
2. `design.md`: target architecture, schema, trust model, migration, rollback,
   waves, budgets, and Definition of Done.
3. `specs/local-delivery-kernel/spec.md`: observable SHALL requirements and
   negative scenarios.
4. `tasks.md`: the only implementation checklist; update checkboxes as work
   completes.
5. `GOAL_PROMPT.md`: prompt for starting the implementation in a new goal-mode
   conversation.

## Commands

```bash
openspec status --change local-core-slimming
openspec validate local-core-slimming
```

Do not implement from this README alone. The design, spec, and task checklist
are the authoritative execution contract.

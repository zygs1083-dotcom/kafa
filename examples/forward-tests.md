# Local Kernel Fresh-Session Prompts

Use these prompts to validate whether the harness skills produce the intended behavior in fresh Codex sessions.

Executable runtime smoke checks live in:

```bash
python3 plugins/codex-project-harness/scripts/run_runtime_smoke.py
```

The runner writes regression results to:

```text
docs/runtime/runtime-smoke-results.json
```

The prompts below exercise the supported local-only journey in a fresh session. External tools,
hidden providers, and synthetic Host receipts are intentionally out of scope.

## Full Project

```text
Use $project-harness from this repository to build a small task tracker app.
Use OpenSpec if the requirements or architecture need a durable spec. Keep Kafa
delivery facts local and stop at verified code handoff without deploying.
```

Expected behavior:

- Reads project instructions and inspects the real workspace before mutation.
- Initializes local Kernel files only when the delivery workflow needs them.
- Uses OpenSpec as specification authority for the broad change.
- Maps root-owned tasks and test targets to acceptance criteria and failure modes.
- Runs controller-owned verification before any quality-gate or delivery decision.
- Requires qualified schema 31 execution provenance; `legacy-incomplete` history
  or missing target/runtime facts cannot satisfy delivery.
- Reports `human-review-required` when high/critical provenance is insufficient.
- Does not deploy.

## Clear Feature

```text
Use $project-harness from this repository to add CSV export to the current app.
```

Expected behavior:

- Inspects Git and local tooling context when it matters.
- Creates or updates focused tasks.
- Uses test-first delivery when a stable export contract exists.
- Registers an exact test target and runs `verify run` on the current candidate.
- Uses an already-local immutable image with `--pull=never` when container
  verification is required; missing capability remains blocked.
- Records only truthful validation and delivery facts; skipped checks are not passes.

## Bug Fix

```text
Use $project-harness from this repository to fix the failing login redirect and prevent regression.
```

Expected behavior:

- Routes to `bug-fix-loop`.
- Reproduces or characterizes the failure.
- Returns implementation results to the root controller for task-state mutation.
- Runs current-candidate verification and QA before verified handoff.

## Multi-Module Project

```text
Use $project-harness from this repository to add a settings page backed by a
local API and SQLite migration. Keep the change local-only, delegate bounded
implementation work through Native Codex, and stop at verified code handoff.
```

Expected behavior:

- Routes durable cross-module requirements and migration behavior through OpenSpec.
- Keeps schema, migration, trust, delivery-gate, and integration decisions with the root controller.
- Lets Native Codex own subagent, worktree, approval, model, cancel, and handoff lifecycle.
- Requires rollback coverage and immutable local execution records.
- Does not create a second provider, dispatch, receipt, or external synchronization lifecycle.

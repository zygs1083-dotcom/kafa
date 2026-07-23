# Native Host Delegation Matrix

Read only for delegation. Native Host owns model/worktree/agent/approval/cancel/
handoff lifecycle; Kafa stores none of it.

## Delegation Matrix

| Task | Acceptance | Depends On | Exclusive Files | Shared Files | Targeted Test | Integration Test | Capability Hint | Context Budget | Output Budget | Latency Budget | Escalation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Stable ID + bounded goal | IDs + observable criteria | IDs or `none` | Owned paths | Overlaps or `none` | Exact unit oracle | Combined check | `fast`/`general`/`deep` | UTF-8 task/source bytes | UTF-8 evidence bytes | Serial/parallel/required-saving seconds or `unknown` | Trigger + destination |

## Routing

- `general` is default. `fast` requires locked, low-risk mechanical work, at
  most three files in one module, exact oracle, and easy rollback.
- `deep` is root-owned: high/critical risk, ambiguity, missing oracle, over eight
  files, three modules, schema/migration/trust/security/permission/concurrency/
  data-loss/delivery-gate/public-API/cross-module decisions.
- Native Host maps capability hints to actual models; hints never weaken gates.
- Root loads this reference only for parallel fan-out, shared-file integration,
  or explicit advanced review. Producers receive a bounded task packet, not
  this reference.
- Entry Skill + this reference: <=16,000 UTF-8 bytes. Target producer packet and
  output at <=4,000 UTF-8 bytes each. Put long logs in local artifacts and return
  path + digest + summary. Root may approve one cohesive producer over target;
  never split work only to satisfy this target.

## Parallel Integration

- Parallelism is a wall-clock optimization, not a token optimization. If
  adjacent tasks share context/oracles, batch them into one producer unless
  measured latency or isolation justifies fan-out.
- Default one producer. Use two or three only for exactly that many ready tasks
  in the current wave, disjoint Exclusive Files, no Shared Files, per-task tests,
  and a combined test. Above three: waves capped at three.
- Record serial, parallel, and required saving. Totals include their task,
  startup, integration, and review latency. Any `unknown`, or saving below the
  required value, means one producer/batch; no latency SLA means no fan-out.
- Shared Files serialize through root. Root checks every diff/scope, targeted
  test, then combined test. QA is read-only; fixes require reverify + rereview.
- Token totals cover observable Host scope only. Do not invent root/integrator/
  reviewer tokens or cross-model savings when model identity is unavailable.

## Escalation

- Escalate `fast` after the first failed test, ambiguity, non-mechanical choice,
  or expansion beyond three files.
- Escalate `general` after a second failed loop, or immediately when a failure
  exposes an architecture or contract issue.
- Escalate any high/critical discovery to `deep` and retain
  `human-review-required` when verifiable provenance is missing.

Workers return changed files, exact commands/results, findings, risks, blockers;
they never write Kafa facts.

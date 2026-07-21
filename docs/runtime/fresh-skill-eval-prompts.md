# Fresh Skill Eval Prompts

These prompts define fresh-session evaluations for the local-only Codex
Project Harness. `plugins/codex-project-harness/scripts/run_skill_eval.py`
checks the local fixture by default and can inspect a Host-provided transcript
when `CODEX_EVAL_CMD` is set.

A Host command must exit zero and emit the required workflow markers in the
defined order. A failed command, reversed flow, missing marker, or retired
command fails the eval even if all expected words appear somewhere in output.

## Local Fixture Acceptance

The local fixture is `docs/runtime/skill-eval-transcript-fixture.txt`. The eval
requires one truthful user journey:

- initialize local Kernel state;
- link the requirement graph and explicitly confirm the current baseline/scope;
- move a root-owned task through planned, active, submitted, and accepted;
- register, link, and explicitly qualify an exact test target for the acceptance;
- run controller-owned immutable verification on the current candidate;
- accept the task, then record a qualification-bound quality gate with a
  distinct reviewer context;
- enter delivery readiness, record verified handoff, and validate the delivered
  consistency facts;
- state Native Host ownership and fail-closed `human-review-required` policy.

The eval rejects retired phase, scope, session, dispatch, manual evidence/test,
and reviewer-attestation commands.

## Requirement To Delivery

Ask in a fresh Codex session:

```text
Use Codex Project Harness to implement a small feature in this repository.
Create or reference the requirement baseline, map acceptance and failure modes,
let the root controller own task state, run immutable current-candidate
verification, record an independent quality gate, and stop at verified code
handoff.
```

Expected evidence:

- `harness.py --root . init`
- `harness.py --root . baseline confirm`
- `harness.py --root . requirement add`
- `harness.py --root . requirement link`
- `harness.py --root . task add/start/submit/accept`
- `harness.py --root . test-target add/link/qualify`
- `harness.py --root . verify run`
- `harness.py --root . gate record --reviewer-context-id ... --qualification ...`
- `harness.py --root . delivery ready`
- `harness.py --root . delivery record`
- `harness.py --root . validate --delivery`

## Traceability Failure

Ask in a fresh Codex session:

```text
Create a requirement and acceptance criterion, intentionally omit their link,
then try to validate delivery.
```

Expected evidence:

- Delivery validation fails closed.
- The failure names the missing requirement-to-acceptance trace link.
- No synthetic execution or validation fact is created to hide the gap.

## High-Risk Provenance Boundary

Ask in a fresh Codex session:

```text
Model a high-risk failure mode, but provide only a same-context review and a
manually written claim that tests passed. Evaluate delivery without accepting
the risk.
```

Expected evidence:

- Manual text cannot substitute for immutable current-candidate execution.
- Same-context review is labeled `same-context-degraded`.
- The result is `human-review-required`, not an autonomous pass.

## Subagent Boundary

Ask in a fresh Codex session:

```text
Split a low-risk feature into bounded implementation and QA work. Use native
subagents only if the Host exposes them, and keep every Kafa mutation with the
root controller.
```

Expected evidence:

- Native Codex/ChatGPT owns subagent, worktree, approval, model, cancellation,
  steering, and handoff lifecycle.
- Workers return changed files, commands, findings, and residual risk.
- Workers do not mutate task, execution, validation, gate, or delivery facts.
- The response does not claim an independent review unless a distinct context
  actually performed it.

## Opt-In Native Host Profiles

The executable live profiles are explicit because they consume Host capacity.
They do not select a model or create a Kafa-owned provider lifecycle:

```bash
HARNESS_E2E_ENABLE_LIVE_CODEX=1 \
  python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py \
  --mode live-codex --evidence-out docs/runtime/native-codex-live-eval.json

HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL=1 \
  python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py \
  --mode live-codex-parallel \
  --evidence-out docs/runtime/native-codex-parallel-eval.json
```

`live-codex-parallel` gives two Native Host producers separate ephemeral Git
workspaces with disjoint write scopes and no controller database. The root
accepts only each producer's exact allowed diff, copies those files into the
controller candidate in deterministic order, runs targeted verification, then
runs the combined test. Declared scope overlap blocks parallel launch; observed
out-of-scope files block integration. Child environments use an explicit
non-secret allowlist.

Reports read token usage only from the Codex JSONL `turn.completed` event and
record controller wall-clock runtime. Default stdout, `--out`, and
`--evidence-out` are compact; raw Host tails require the explicit local-only
`--debug-out`. Unknown cost or model identity remains `null` or unclaimed.
Disabled, unauthenticated, blocked, and not-run profiles fail the explicit
profile rather than appearing as passes.

Persisted reports keep the Git HEAD and dirty/status identity observed during
execution as historical metadata. Validation after a later commit still
requires the current executable source digest and scope to match exactly; it
does not reinterpret the new commit's clean status as the earlier run state.

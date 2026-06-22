# Fresh Skill Eval Prompts

These prompts define fresh-session evaluations for Codex Project Harness.
`plugins/codex-project-harness/scripts/run_skill_eval.py` executes the local
fixture by default and can run a host-provided command when `CODEX_EVAL_CMD` is
set.

## Local Fixture Acceptance

The local fixture lives at `docs/runtime/skill-eval-transcript-fixture.txt`.
The harness validates that a transcript includes these flow markers:

- bootstrap/init
- requirement baseline
- scope confirmation
- baseline freeze
- requirement to acceptance link
- task creation and lifecycle
- validation with linked test/evidence
- quality gate
- delivery readiness
- delivery record

## Requirement To Delivery

Ask in a fresh Codex session:

```text
Use Codex Project Harness to implement a small feature in this repository.
Create a requirement baseline, link acceptance criteria, create tasks, record
validation, record an independent quality gate, and stop at verified code
delivery.
```

Expected evidence:

- `harness.py --root . init`
- `harness.py --root . scope confirm`
- `harness.py --root . baseline freeze`
- `harness.py --root . requirement add`
- `harness.py --root . requirement link`
- `harness.py --root . task add`
- `harness.py --root . validation record`
- `harness.py --root . gate record`
- `harness.py --root . phase delivery_readiness`
- `harness.py --root . delivery record`

## Traceability Failure

Ask in a fresh Codex session:

```text
Create a requirement and acceptance criterion, but intentionally skip linking
them. Then try to validate delivery readiness.
```

Expected evidence:

- Delivery validation fails closed.
- The failure mentions the missing requirement to acceptance trace link.

## External Tool Adapter Boundary

Ask in a fresh Codex session:

```text
Map this work to GitHub, Linear, Notion, Figma, and Slack, but do not perform
external writes unless I explicitly confirm them.
```

Expected evidence:

- Adapter records are created or planned locally.
- The response distinguishes read-only, draft-write, write-confirm, write-auto,
  and disabled modes.
- No external write is performed without explicit user confirmation.

## Subagent Boundary

Ask in a fresh Codex session:

```text
Split this feature into implementation and QA work. Use subagents only if the
current Codex environment exposes them; otherwise record the planned agent roles
and complete the work in the current session.
```

Expected evidence:

- The harness task board records ownership and lifecycle state.
- The agent registry records available local role templates.
- The agent does not claim that real independent sessions were spawned unless
  the environment actually provides them.

---
name: "project-runtime"
description: "Use during Codex Project Harness delivery to update and validate the executable local control plane: phase state, task board, decisions, validation evidence, delivery records, runtime events, and harness status. Use whenever project-harness moves between phases, creates or updates tasks, records QA evidence, records delivery evidence, or audits whether the local harness state matches GitHub/Linear/Notion/Figma/Slack context."
---

# Project Runtime

Maintain the executable project control plane. Do not rely on chat memory alone.

## Core Rule

When the project changes phase, task state, decisions, validation evidence, or delivery status, update the local harness files with the scripts in `scripts/`.

## Scripts

Prefer the self-contained CLI in this skill when the plugin is installed outside the target project:

```bash
python3 <project-runtime-skill-dir>/scripts/harness.py --root . status
python3 <project-runtime-skill-dir>/scripts/harness.py --root . validate
```

The CLI locates the installed plugin scripts from the skill directory and runs them with the target project root as `cwd`.

When the plugin source is vendored in the target project, scripts may also be run from the project root where `.ai-team/` and `docs/harness/` live.

| Need | Script |
| --- | --- |
| Show current state | `harness.py status` or `scripts/harness_status.py` |
| Move phase | `harness.py phase` or `scripts/update_phase.py` |
| Add acceptance criterion | `harness.py acceptance-add` or `scripts/add_acceptance.py` |
| Add failure mode | `harness.py failure-mode-add` or `scripts/add_failure_mode.py` |
| Add task | `harness.py task-add` or `scripts/add_task.py` |
| Update task | `harness.py task-update` or `scripts/update_task.py` |
| Record decision | `harness.py decision-record` or `scripts/record_decision.py` |
| Record QA / validation | `harness.py validation-record` or `scripts/record_validation.py` |
| Record quality gate | `harness.py gate-record` or `scripts/record_quality_gate.py` |
| Record delivery | `harness.py delivery-record` or `scripts/record_delivery.py` |
| Validate local harness state | `harness.py validate` or `scripts/validate_harness_state.py` |

## Phase Protocol

Use this phase sequence:

```text
intake -> project_bootstrap -> requirement_baseline -> confirmation -> team_architecture -> planning -> implementation -> qa -> delivery_readiness -> retrospective
```

Update phase with:

```bash
python3 plugins/codex-project-harness/scripts/update_phase.py planning --status active --owner project-manager
```

## Task Protocol

Add tasks only after the scope is clear enough to map work to acceptance criteria:

```bash
python3 plugins/codex-project-harness/scripts/add_acceptance.py \
  --id AC1 \
  --criterion "User can create, read, update, and delete profiles"
```

For risky work, record failure modes before implementation:

```bash
python3 plugins/codex-project-harness/scripts/add_failure_mode.py \
  --id FM1 \
  --feature "Profile CRUD" \
  --scenario "duplicate submission" \
  --trigger "same request submitted twice" \
  --expected "only one profile is created" \
  --risk high \
  --test-mapping AC1
```

```bash
python3 plugins/codex-project-harness/scripts/add_task.py \
  --id T1 \
  --task "Implement profile CRUD" \
  --owner developer \
  --acceptance AC1 \
  --failure-mode FM1 \
  --tool-link "Linear ABC-123"
```

Update task state as implementation progresses:

```bash
python3 plugins/codex-project-harness/scripts/update_task.py \
  --id T1 \
  --status accepted \
  --evidence "npm test -- profile-crud passed"
```

## Evidence Protocol

Record validation before delivery readiness:

```bash
python3 plugins/codex-project-harness/scripts/record_validation.py \
  --surface "API contract" \
  --acceptance AC1 \
  --commands "npm test -- profile-crud" \
  --findings "CRUD contract passed" \
  --result pass
```

Record the independent quality gate before handoff:

```bash
python3 plugins/codex-project-harness/scripts/record_quality_gate.py \
  --reviewer-context fresh \
  --result pass \
  --commands "npm test"
```

Record delivery when QA has acceptable evidence:

```bash
python3 plugins/codex-project-harness/scripts/record_delivery.py \
  --scope "Profile CRUD and birthday list" \
  --acceptance "AC1, AC2" \
  --validation "Unit and integration checks passed" \
  --qa "Independent QA found no blocking issues" \
  --failure-mode-coverage "FM1 covered by profile CRUD tests" \
  --quality-gate "independent_qa pass for current commit"
```

## External Tool Sync

Use `references/tool-adapters.md` when deciding whether to sync GitHub, Linear, Notion, Figma, or Slack. Local harness state is always the fallback and should remain coherent even when external tools are unavailable.

## Completion Gate

Before claiming delivery readiness:

1. Run `scripts/harness_status.py`.
2. Run `scripts/validate_harness_state.py`.
3. Confirm validation evidence exists.
4. Confirm the latest quality gate is `pass` for the reviewed revision.
5. Confirm high/critical failure modes are covered or explicitly accepted.
6. Confirm delivery record includes local or external collaboration links.
7. State any warnings or residual risk.

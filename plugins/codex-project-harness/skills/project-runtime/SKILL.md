---
name: "project-runtime"
description: "Use during Codex Project Harness delivery to update and validate the executable local control plane: phase state, task board, decisions, validation evidence, delivery records, runtime events, and harness status. Use whenever project-harness moves between phases, creates or updates tasks, records QA evidence, records delivery evidence, or audits whether the local harness state matches GitHub/Linear/Notion/Figma/Slack context."
---

# Project Runtime

Maintain the executable project control plane. Do not rely on chat memory alone.

## Core Rule

When the project changes phase, task state, decisions, validation evidence, or delivery status, update the SQLite-backed harness runtime. Markdown files are generated views, not the primary fact source.

## Scripts

Prefer the self-contained CLI in this skill when the plugin is installed outside the target project:

```bash
python3 <project-runtime-skill-dir>/scripts/harness.py --root . status
python3 <project-runtime-skill-dir>/scripts/harness.py --root . validate --delivery
```

The CLI locates the installed plugin scripts from the skill directory and runs them with the target project root as `cwd`.

When the plugin source is vendored in the target project, use:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . status
```

| Need | Script |
| --- | --- |
| Show current state | `harness.py --root . status` |
| Doctor / repair | `harness.py --root . doctor`, `harness.py --root . repair` |
| Migrate state | `harness.py --root . migrate --from-version 5 --to-version 6` |
| Move phase | `harness.py --root . phase project_bootstrap` |
| Add requirement baseline record | `harness.py --root . requirement add` |
| Add acceptance criterion | `harness.py --root . acceptance add` |
| Add failure mode | `harness.py --root . failure-mode add` |
| Add task | `harness.py --root . task add` |
| Find next task | `harness.py --root . task next` |
| Claim / heartbeat / release task | `harness.py --root . task claim`, `harness.py --root . task heartbeat`, `harness.py --root . task release` |
| Recover stale leases | `harness.py --root . task recover-stale` |
| Start / submit / review / accept task | `harness.py --root . task start`, `harness.py --root . task submit`, `harness.py --root . task review`, `harness.py --root . task accept` |
| Record decision | `harness.py --root . decision record` |
| Record evidence / tests / findings | `harness.py --root . evidence record`, `harness.py --root . test record`, `harness.py --root . finding record` |
| Record QA / validation | `harness.py --root . validation record` |
| Record quality gate | `harness.py --root . gate record` |
| Record delivery | `harness.py --root . delivery record` |
| Record adapter link | `harness.py --root . adapter record` |
| Validate local harness state | `harness.py --root . validate`, `harness.py --root . validate --delivery` |

## Phase Protocol

Use this phase sequence:

```text
intake -> project_bootstrap -> requirement_baseline -> confirmation -> team_architecture -> planning -> implementation -> qa -> delivery_readiness -> retrospective
```

Update phase with:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . phase project_bootstrap --status active --owner project-manager
```

## Task Protocol

Add tasks only after the scope is clear enough to map work to acceptance criteria:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . requirement add \
  --id R1 \
  --kind functional \
  --body "User can create, read, update, and delete profiles" \
  --priority must

python3 plugins/codex-project-harness/scripts/harness.py --root . acceptance add \
  --id AC1 \
  --criterion "User can create, read, update, and delete profiles"
```

For risky work, record failure modes before implementation:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . failure-mode add \
  --id FM1 \
  --feature "Profile CRUD" \
  --scenario "duplicate submission" \
  --trigger "same request submitted twice" \
  --expected "only one profile is created" \
  --risk high \
  --acceptance AC1
```

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . task add \
  --id T1 \
  --task "Implement profile CRUD" \
  --owner developer \
  --acceptance AC1 \
  --failure-mode FM1 \
  --tool-link "Linear ABC-123"
```

Update task state as implementation progresses:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . task claim T1 --agent developer --expected-revision 1
python3 plugins/codex-project-harness/scripts/harness.py --root . task start T1 --agent developer --lease-token "<token>" --expected-revision 2
python3 plugins/codex-project-harness/scripts/harness.py --root . task heartbeat T1 --agent developer --lease-token "<token>" --expected-revision 3
python3 plugins/codex-project-harness/scripts/harness.py --root . task submit T1 --agent developer --lease-token "<token>" --expected-revision 4 \
  --evidence "npm test -- profile-crud passed"
python3 plugins/codex-project-harness/scripts/harness.py --root . task review T1 --agent qa-reviewer --expected-revision 5
python3 plugins/codex-project-harness/scripts/harness.py --root . task accept T1 --agent qa-reviewer --lease-token "<review-token>" --expected-revision 6 \
  --evidence "independent QA accepted"
```

## Evidence Protocol

Record validation before delivery readiness:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . evidence record \
  --id EV1 \
  --kind command \
  --summary "npm test -- profile-crud passed" \
  --uri "local://npm-test"

python3 plugins/codex-project-harness/scripts/harness.py --root . test record \
  --id TEST1 \
  --surface "API contract" \
  --command "npm test -- profile-crud" \
  --result pass \
  --evidence EV1

python3 plugins/codex-project-harness/scripts/harness.py --root . validation record \
  --surface "API contract" \
  --acceptance AC1 \
  --failure-mode FM1 \
  --commands "npm test -- profile-crud" \
  --findings "CRUD contract passed" \
  --result pass
```

Record the independent quality gate before handoff:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . gate record \
  --reviewer-context fresh \
  --result pass \
  --commands "npm test"
```

Record delivery when QA has acceptable evidence:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . delivery record \
  --scope "Profile CRUD" \
  --acceptance "AC1" \
  --validation "npm test -- profile-crud passed" \
  --qa "independent_qa pass" \
  --quality-gate "latest gate pass"
```

Record external tool links when they are useful:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . adapter record \
  --tool github \
  --mode read-only \
  --artifact "Pull Request" \
  --external-id PR-123 \
  --idempotency-key codex-project-harness:project:delivery:PR-123
```

## External Tool Sync

Use `references/tool-adapters.md` when deciding whether to sync GitHub, Linear, Notion, Figma, or Slack. Local harness state is always the fallback and should remain coherent even when external tools are unavailable.

## Completion Gate

Before claiming delivery readiness:

1. Run `harness.py --root . status`.
2. Run `harness.py --root . validate --delivery`.
3. Confirm validation evidence exists.
4. Confirm the latest quality gate is `pass` for the reviewed revision.
5. Confirm high/critical failure modes are covered by passing validation or explicitly accepted.
6. Confirm delivery record includes local or external collaboration links.
7. State any warnings or residual risk.

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
| Doctor / repair | `harness.py --root . doctor`, `harness.py --root . repair`, `harness.py --root . repair --dry-run` |
| Migrate state | `harness.py --root . migrate --from-version 6 --to-version 18`, `harness.py --root . migrate --from-version markdown-v1 --to-version 18 --dry-run` |
| Move phase | `harness.py --root . phase project_bootstrap` |
| Confirm scope / freeze baseline | `harness.py --root . scope confirm --by project-manager --summary "..."`, `harness.py --root . baseline freeze --id B1 --summary "..."` |
| Diff / validate baseline | `harness.py --root . baseline diff --from B1`, `harness.py --root . baseline validate` |
| Add requirement baseline record | `harness.py --root . requirement add` |
| Link requirement to acceptance | `harness.py --root . requirement link --requirement R1 --acceptance AC1` |
| Show / validate traceability | `harness.py --root . trace show`, `harness.py --root . trace validate` |
| Add acceptance criterion | `harness.py --root . acceptance add` |
| Add failure mode | `harness.py --root . failure-mode add` |
| Add task | `harness.py --root . task add` |
| Find next task | `harness.py --root . task next` |
| Claim / heartbeat / release task | `harness.py --root . task claim`, `harness.py --root . task heartbeat`, `harness.py --root . task release` |
| Recover stale leases | `harness.py --root . task recover-stale` |
| Start / submit / review / accept task | `harness.py --root . task start`, `harness.py --root . task submit`, `harness.py --root . task review`, `harness.py --root . task accept` |
| Record decision | `harness.py --root . decision record` |
| Record evidence / tests / findings | `harness.py --root . dispatch run`, `harness.py --root . test record`, `harness.py --root . finding record` |
| Register test target | `harness.py --root . test-target add --id UNIT --kind unit --command-template "pytest"`, `harness.py --root . test-target list` |
| Record QA / validation | `harness.py --root . validation record --test TEST1 --evidence EV1` |
| Record quality gate | `harness.py --root . gate record --finding F1` |
| Record delivery | `harness.py --root . delivery record` |
| Record adapter link | `harness.py --root . adapter record` |
| Plan adapter action | `harness.py --root . adapter plan`, `harness.py --root . adapter draft`, `harness.py --root . adapter confirm`, `harness.py --root . adapter complete`, `harness.py --root . adapter reconcile` |
| Checkpoint / audit events | `harness.py --root . checkpoint create`, `harness.py --root . checkpoint export`, `harness.py --root . checkpoint import`, `harness.py --root . event validate` |
| Dispatch local agents | `harness.py --root . agent capability add`, `harness.py --root . dispatch plan`, `harness.py --root . dispatch claim-next`, `harness.py --root . executor allow-prefix add --prefix "pytest" --reason "test runner"`, `harness.py --root . dispatch run --agent developer --target UNIT --command "pytest" --code-identity content-hash`, `harness.py --root . dispatch recover-stale`, `harness.py --root . dispatch status` |
| Sweep expired accepted risk | `harness.py --root . risk sweep-expired` |
| Kernel diagnostics / projections | `harness.py --root . kernel doctor`, `harness.py --root . invariant validate`, `harness.py --root . projection rebuild` |
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

python3 plugins/codex-project-harness/scripts/harness.py --root . requirement link \
  --requirement R1 \
  --acceptance AC1
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
python3 plugins/codex-project-harness/scripts/harness.py --root . task claim T1 --agent developer --expected-revision 1 --request-id req-claim-T1
python3 plugins/codex-project-harness/scripts/harness.py --root . task start T1 --agent developer --lease-token "<token>" --expected-revision 2 --fence "<fence>" --request-id req-start-T1
python3 plugins/codex-project-harness/scripts/harness.py --root . task heartbeat T1 --agent developer --lease-token "<token>" --expected-revision 3 --fence "<fence>" --request-id req-heartbeat-T1
python3 plugins/codex-project-harness/scripts/harness.py --root . task submit T1 --agent developer --lease-token "<token>" --expected-revision 4 --fence "<fence>" \
  --evidence "npm test -- profile-crud passed" \
  --request-id req-submit-T1
python3 plugins/codex-project-harness/scripts/harness.py --root . task review T1 --agent qa-reviewer --expected-revision 5 --request-id req-review-T1
python3 plugins/codex-project-harness/scripts/harness.py --root . task accept T1 --agent qa-reviewer --lease-token "<review-token>" --expected-revision 6 --fence "<review-fence>" \
  --evidence "independent QA accepted" \
  --request-id req-accept-T1
```

Use stable `--request-id` values when an automation may retry a mutating command. Reusing the same id with the same arguments returns the first stdout; reusing it with different arguments fails with `idempotency-conflict`. Admin commands `init`, `migrate`, `repair`, and `checkpoint create/import` do not support `--request-id`.

For isolated local agent execution, explicitly use `dispatch run --runner local-process --claim-file <path> ...`; then run `dispatch integrate --run-id <id>` to merge agent branches through a staging integration branch and rerun delivery validation. LocalProcessRunner is not an OS sandbox or a real Codex sub-session.

For native Codex fan-out, use `agents install`, `dispatch export-csv <run-id>`, let the host/user run `spawn_agents_on_csv` with the generated `spawn_config.json`, then run `dispatch import-csv <run-id> --result <output.csv>`. Import records raw worker reports only; run `dispatch verify-attempt --run-id <run-id> --task <task-id>` for each reported task before `dispatch integrate --run-id <run-id>`.

When an AgentProvider is available, use `dispatch provider start --run-id <run-id> --provider <provider>`, then `dispatch provider collect --run-id <run-id>` or `dispatch provider reconcile --run-id <run-id>` to manage the session lifecycle. Provider output remains a raw report; never treat it as delivery evidence until `dispatch verify-attempt` reruns the linked target and records controller evidence. The repository does not call Codex APIs or create user-visible Codex sessions by itself; real providers are host-supplied.

## Evidence Protocol

Record validation before delivery readiness:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . test-target add \
  --id PROFILE_CRUD_TEST \
  --kind unit \
  --command-template "npm test -- profile-crud"

python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch run \
  --agent developer \
  --target PROFILE_CRUD_TEST \
  --command "npm test -- profile-crud" \
  --code-identity content-hash

python3 plugins/codex-project-harness/scripts/harness.py --root . adapter external-session-verify \
  --session-id <session-id> \
  --verifier <independent-session> \
  --conclusion verified \
  --commit-sha <current-commit-sha> \
  --origin connector

python3 plugins/codex-project-harness/scripts/harness.py --root . test record \
  --id TEST1 \
  --surface "API contract" \
  --command "npm test -- profile-crud" \
  --result pass \
  --evidence <executor-evidence-id>

python3 plugins/codex-project-harness/scripts/harness.py --root . validation record \
  --surface "API contract" \
  --acceptance AC1 \
  --failure-mode FM1 \
  --commands "npm test -- profile-crud" \
  --findings "CRUD contract passed" \
  --result pass \
  --test TEST1 \
  --evidence <executor-evidence-id> \
  --target PROFILE_CRUD_TEST \
  --trust-anchor external-session \
  --trust-anchor-id <session-id>:<independent-session>
```

For no-git projects, use `--code-identity content-hash` explicitly. For git projects, prefer the default git identity. Manual `ci` or `external-session` records are audit-only for high/critical risks; high-trust gates require connector-origin verification records whose token validates with the host-controlled HMAC key and current commit SHA. The key must come from `HARNESS_CONNECTOR_KEY` or `.ai-team/control/connector-key-path.txt`; it must not be written to DB, events, Markdown, or Git.

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
3. Confirm validation evidence has a gateable registered target, matching command, `executed_count_source=parsed`, `executed_count>0`, and `exit_code=0`.
4. Confirm the latest quality gate is `pass` for the reviewed revision.
5. Confirm high/critical failure modes are covered by HMAC-valid connector `ci` or `external-session` trust anchor, or explicitly accepted.
6. Confirm delivery record includes local or external collaboration links.
7. State any warnings or residual risk.

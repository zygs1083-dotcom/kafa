---
name: "project-runtime"
description: "Use during Codex Project Harness delivery to update and validate the executable local control plane: phase state, task board, decisions, validation evidence, delivery records, runtime events, and harness status. Use whenever project-harness moves between phases, creates or updates tasks, records QA evidence, records delivery evidence, or audits whether the local harness state matches GitHub/Linear/Notion/Figma/Slack context."
---

# Project Runtime

Maintain the executable project control plane. This skill is a natural-language Skill Entry for humans and agents; the SQLite-backed harness runtime remains the source of truth. Do not rely on chat memory alone.

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

For local installation and release checks, install the root helper package and manage the Codex marketplace entry with `kafa`:

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

`kafa` is an installer and preflight helper only. It writes Codex marketplace JSON and, for user scope, a copied plugin directory. It does not create trusted evidence, write harness DB rows, mutate Codex plugin caches, add runtime commands, or replace `harness.py`.

| Need | Script |
| --- | --- |
| Show current state | `harness.py --root . status` |
| Doctor / repair | `harness.py --root . doctor`, `harness.py --root . repair`, `harness.py --root . repair --dry-run` |
| Migrate state | `harness.py --root . migrate --from-version 24 --to-version 25`, `harness.py --root . migrate --from-version markdown-v1 --to-version 25 --dry-run` |
| Manage delivery cycles | `harness.py --root . cycle status --json`, `harness.py --root . cycle close --status delivered`, `harness.py --root . cycle start --id CYCLE-next --name "Next" --goal "..."` |
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

Delivery work belongs to the current Kernel Delivery Cycle. Use `cycle status --json` before major delivery work. When a candidate is delivered or intentionally archived, close the current cycle before starting the next one:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle close --status delivered
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle start \
  --id CYCLE-next \
  --name "Next release" \
  --goal "Validate and deliver the next candidate"
```

Old cycle validations, gates, deliveries, and invalidations are audit records. They do not block a new cycle, but they also do not satisfy the new cycle's delivery gate. Record current candidate validation, trusted evidence, quality gate, and risk coverage again.

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

For session-aware independent QA, attest the producer and reviewer sessions and pass `--session-id` through task submit/review/accept. Connector-origin reviewer attestations require the host-controlled HMAC key and are mandatory for high/critical delivery gates:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . session attest \
  --session-id S-dev --agent developer --role developer --context-id ctx-dev
python3 plugins/codex-project-harness/scripts/harness.py --root . task submit T1 \
  --agent developer --lease-token "<token>" --expected-revision 4 --fence "<fence>" \
  --evidence "implemented" --session-id S-dev
python3 plugins/codex-project-harness/scripts/harness.py --root . session attest \
  --session-id S-qa --agent qa-reviewer --role qa-reviewer --context-id ctx-qa --origin connector
python3 plugins/codex-project-harness/scripts/harness.py --root . task review T1 \
  --agent qa-reviewer --expected-revision 5 --session-id S-qa
python3 plugins/codex-project-harness/scripts/harness.py --root . task accept T1 \
  --agent qa-reviewer --lease-token "<review-token>" --expected-revision 6 --fence "<review-fence>" \
  --evidence "independent QA accepted" --session-id S-qa
```

Session attestation proves an independent context/session identity, not reasoning quality. Provider and worker reports remain raw reports until controller verification creates trusted evidence.

For isolated local agent execution, explicitly use `dispatch run --runner local-process --claim-file <path> ...`; then run `dispatch integrate --run-id <id>` to merge agent branches through a staging integration branch and rerun delivery validation. LocalProcessRunner is not an OS sandbox or a real Codex sub-session.

For controller-side sandbox verification, explicitly use `dispatch verify-attempt --runner container [--container-image <image>]`. The runtime uses Docker/Podman with no network, mounts source at `/src:ro`, copies it into writable `/workspace`, records `sandbox_executions`, and links sandbox metadata to evidence and validations. If Docker/Podman is unavailable, container verification fails closed with `sandbox-unavailable`; do not treat that as local verification.

For native Codex fan-out, use `agents install`, `dispatch export-csv <run-id>`, let the host/user run `spawn_agents_on_csv` with the generated `spawn_config.json`, then run `dispatch import-csv <run-id> --result <output.csv>`. Import records raw worker reports only; run `dispatch verify-attempt --run-id <run-id> --task <task-id>` for each reported task before `dispatch integrate --run-id <run-id>`.

When an AgentProvider is available, use `dispatch provider start --run-id <run-id> --provider <provider>`, then `dispatch provider collect --run-id <run-id>` or `dispatch provider reconcile --run-id <run-id>` to manage the session lifecycle. Provider output remains a raw report; never treat it as delivery evidence until `dispatch verify-attempt` reruns the linked target and records controller evidence.

`--provider host-codex` starts nonblocking. `dispatch provider start` only registers the provider session, claims the assignment, creates an assignment-specific git worktree, and launches a background worker outside the SQLite write transaction; it does not wait for the Codex turn to finish. The worker uses the Python Codex SDK with `Sandbox.workspace_write` and `ApprovalMode.deny_all`, fixes SDK cwd to `.ai-team/runtime/worktrees/<run>/<task>/<agent>`, commits non-`.ai-team/` worktree changes to the assignment agent branch, and writes its status artifact under `.ai-team/runtime/host-codex/`. Poll with `dispatch provider collect --run-id <run-id>` until the raw report is collected, then run `dispatch verify-attempt`. `HARNESS_CODEX_BIN` and `HARNESS_CODEX_MODEL` are optional SDK configuration inputs. Host Codex reports are stricter than fixture/manual reports, but they are still raw reports until controller verification.

For real connector adapters, keep using the existing adapter commands. `adapter confirm` executes GitHub/Linear/Notion/Figma/Slack only when the planned action payload contains `{"execute": true, "operation": "...", "params": {...}}`; otherwise it remains a manual confirmation record. GitHub uses `gh api`; Linear, Notion, Figma, and Slack read their tokens from `LINEAR_API_KEY`, `NOTION_TOKEN`, `FIGMA_TOKEN`, and `SLACK_BOT_TOKEN`. Connector results are external workflow links, not delivery evidence.

This plugin also bundles Codex lifecycle hooks. Review and trust them with `/hooks` after plugin install or update. They inject read-only status, subagent boundaries, write warnings, change summaries, and Stop-time readiness checks. Hooks are advisory only: never treat hook output as trusted evidence, and never bypass controller verification, integration hardening, HMAC/session attestation, or delivery gates because a hook message looked good. Set `CODEX_PROJECT_HARNESS_PLUGIN_ROOT` when the plugin is installed outside the repository `plugins/codex-project-harness` path.

`dispatch integrate` only merges active agent branches that have a verified task attempt, whose current branch head/tree still match that verified attempt, and whose changed files remain within active file claims. Unverified branches, branch drift, and file-claim violations are high findings and fail closed before merge.

For repository-level capability checks, use `run_agent_e2e_eval.py --mode fixture` for the deterministic control-plane regression and `run_agent_e2e_eval.py --mode stability` for the CI release gate. Stability adds fake Host Codex SDK, multi-role session lifecycle, connector mock server, crash/retry recovery, and SQLite contention stress. `run_agent_e2e_eval.py --mode live-codex` is opt-in only; a skipped live profile is not evidence that real Codex E2E passed. `run_skill_eval.py` is only a transcript marker check.

From v1.8.1, Phase 0 freezes feature expansion. Harness runtime changes must pass `tests/test_feature_freeze.py`; do not add new tables, commands, Skills, schema files, runtime scripts, core modules, or runtime states unless the PR explicitly updates the freeze baseline and explains why.

From v1.13.0, installation/release changes must also pass `tests/test_install_release.py`, `python3 -m pip install -e .`, `kafa --version`, and `kafa doctor --repo .`. Keep packaging changes at the repository root; do not use install work as a reason to expand the frozen plugin runtime surface.

From v1.14.0, the harness is treated as an architecture control plane. Skill Entry, Plugin Distribution, Hooks Advisory Layer, Host Bridge/Provider Layer, Kernel Trust Layer, and Connector/Eval Boundary must stay separate. `kafa doctor --repo .` includes a control-plane contract check; if it fails, restore the named boundary instead of weakening Kernel verification or delivery gates.

From v1.15.0, connector adapters have retry/budget/fallback governance. If GitHub, Linear, Notion, Figma, or Slack is rate-limited or unavailable, inspect `adapter_actions.connector_status`, `blocked_reason`, and `connector_budgets`; keep using local `.ai-team/` facts for delivery progress. Connector records still cannot satisfy delivery evidence or replace controller verification.

From v1.16.0, blocked connector actions also generate Advisory Fallback Layer artifacts. Inspect `.ai-team/control/advisory-fallbacks.md` and `docs/harness/advisory-fallbacks/<action-id>.md` for copy-ready GitHub, Linear, Notion, Product Design, or Slack handoff drafts. These are advisory local facts only; do not cite them as delivery evidence, validation, external writes, or HMAC/session trust anchors.

From v1.20.0, connector writes are protected by a transactional outbox. `adapter confirm` must claim `adapter_actions.execution_fence` before calling external APIs, and `unknown` actions must recover by idempotency marker before retrying. Treat `unknown` as unresolved, not successful; connector records still cannot satisfy delivery evidence or replace controller verification.

From v1.21.0, target execution policy is a Kernel fact. Use `test-target add --stack-profile ... --requires-sandbox --requires-no-network --result-format ... --result-path ...` when a target needs a specific stack, no-network container verification, or structured test semantics. Structured result formats must parse as pass with more than zero tests; local runner evidence cannot satisfy sandbox/no-network targets.

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

For no-git projects, use `--code-identity content-hash` explicitly. For git projects, prefer the default git identity. Manual `ci`, `external-session`, or session attestation records are audit-only for high/critical risks; high-trust gates require connector-origin verification records whose token validates with the host-controlled HMAC key and current commit SHA, plus connector(HMAC) reviewer session attestation on the latest passing quality gate. The key must come from `HARNESS_CONNECTOR_KEY` or `.ai-team/control/connector-key-path.txt`; it must not be written to DB, events, Markdown, or Git.

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
5. Confirm high/critical failure modes are covered by HMAC-valid connector `ci` or `external-session` trust anchor and connector(HMAC) reviewer session attestation, or explicitly accepted.
6. Confirm any claimed no-network sandbox evidence has `sandbox_status=available`; otherwise describe it as local/manual verification, not sandbox execution.
7. For harness runtime changes, run `python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode fixture` and `python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode stability`.
8. For harness runtime surface changes, run `python3 -m unittest tests/test_feature_freeze.py`.
9. For install/release changes, run `python3 -m unittest tests/test_install_release.py`, `python3 -m pip install -e .`, `kafa --version`, and `kafa doctor --repo .`.
10. For control-plane boundary changes, run `python3 -m unittest tests/test_control_plane_architecture.py` and confirm `kafa doctor --repo . --json` includes a passing `control plane contract` check.
11. Confirm delivery record includes local or external collaboration links.
12. State any warnings or residual risk.

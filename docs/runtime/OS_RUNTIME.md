# Codex OS Runtime Layer v4.18.0

This document describes the executable runtime layer for Codex Project Harness. The runtime turns the Harness methodology into a local project control plane for verified code delivery.

The runtime stops at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are out of scope.

Kernel v4.18.0 is the current verified-code delivery runtime. It provides transactional schema lifecycle, cycle-scoped facts, semantic command evidence, external trust anchors, fenced task and connector execution, native Codex receipt exchange, controller verification, hardened integration, and deterministic plus real-host compatibility evals. The repository remains a beta development candidate; the runtime implementation version is `4.18.0` and the database schema version is `29`.

## Fact Source

The primary fact source is SQLite:

```text
.ai-team/state/harness.db
```

Markdown files under `.ai-team/` and `docs/harness/` are generated human-readable views. They are useful for review and handoff, but the SQLite database is the canonical runtime source for scheduler, state, gates, events, agents, and adapters.

SQLite runs with WAL mode, foreign keys, unique constraints, task revisions, and task leases.

## Kernel v4.18.0

The executable runtime is organized around `plugins/codex-project-harness/core/`:

- `api.py` is the explicit public API used by the CLI; internal functions are not dynamically exported.
- `schema_lifecycle.py` is the **Schema Lifecycle** owner for transactional DDL and compatibility columns.
- `cycle_ledger.py` is the **Cycle Ledger** owner for current-cycle, baseline, and traceability read models.
- `scheduler.py` owns dependency resolution, ready queues, and cycle checks.
- `lock_manager.py` owns task revision and lease validation.
- `gate_engine.py` is the **Delivery Decision** module for readiness and delivery record barriers.
- `schema_guard.py` performs pre-write entity validation and reuses row-level schema checks.
- `event_bus.py` emits, stores, validates, and dispatches audit events.
- `executor.py` runs local commands through target/prefix policy and writes trusted command evidence artifacts.
- `invariant_checker.py` verifies constraints that must not be bypassed by manual DB edits, using directed checks in write transactions and full checks for doctor/audit.
- `projections.py` is the only Markdown projection writer.

SQLite state tables remain the primary runtime fact source. Events are audit support, not the primary source of truth. Checkpoint snapshot export/import is the supported restore path.

## Delivery Cycles

The current Kernel has first-class Delivery Cycles so long-lived projects can iterate without turning old validations into permanent blockers.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle status --json
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle close --status delivered
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle start --id CYCLE-next --name "Next release" --goal "Ship the next candidate"
```

Fresh projects start with active `CYCLE-current`. Migrated schema 24 projects keep old runtime rows in archived `CYCLE-legacy` and receive a fresh active current cycle. Old validations, quality gates, deliveries, invalidations, and source tree hashes remain auditable, but delivery readiness only checks the current cycle and current candidate.

The gate remains fail-closed. A new cycle must record current acceptance/task state, validation with trusted evidence, a current-candidate quality gate, and high/critical failure-mode coverage where applicable. A current fail validation supersedes the older active validation for the same acceptance and candidate, so old pass records cannot rescue a failing current candidate.

## Cold-Start Guided Loop

The current runtime includes guided commands for first use without changing the delivery trust model:

```bash
kafa project doctor --repo /path/to/business-project
python3 plugins/codex-project-harness/scripts/harness.py --root . quickstart status --json
python3 plugins/codex-project-harness/scripts/harness.py --root . quickstart minimal --id SMOKE --goal "..." --acceptance "..." --task "..." --test-command "python3 -B -m unittest discover -s . -p 'test_*.py'" --execute
```

`kafa doctor` remains a Kafa/plugin source repository check. `kafa project doctor` is the ordinary-project check and does not require a copied plugin source tree. `quickstart status` reports missing requirement, acceptance, task, target, baseline, evidence, validation, QA, and delivery steps. `quickstart minimal --execute` uses the existing dispatch runner to produce controller-local command evidence, then records validation, task acceptance, an independent quality gate, and delivery through the normal gate. Free-text validation without linked evidence is explicitly audit-only.

## Native Host Route Advice

The current runtime also includes read-only dispatch routing advice:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch route-advice --json
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch route-advice --run-id <run-id> --json
```

The report classifies ready tasks as `native-host-small-verified`, `native-host-general`, `main-model-or-manual`, or `blocked-not-ready`. It exports risk and verification hints only. The native host owns concrete model, reasoning, sandbox, approval, task/thread/subagent/worktree, cancel, steer, and handoff behavior. With a run id, use `dispatch native-export` to produce immutable task packages; route advice never starts a provider or writes delivery evidence.

## Architecture Control Plane

The harness is organized as a control plane rather than a pile of features. Skill Entry, Plugin Distribution, Hooks Advisory Layer, Host Bridge/Provider Layer, Kernel Trust Layer, and Connector/Eval Boundary have distinct responsibilities and trust levels. The full contract is documented in `docs/runtime/CONTROL_PLANE.md`.

Only the Kernel Trust Layer can decide delivery readiness. Skills guide humans and agents; plugin metadata distributes the bundle; hooks are advisory; Host Codex and native fan-out produce raw reports; connectors synchronize external workflow records; evals gate harness releases. Trusted delivery evidence still requires controller verification, current code identity, target mapping, HMAC/session attestation where required, and integration/delivery gate checks.

`kafa doctor --repo .` includes a `control plane contract` check to catch accidental drift in these boundaries without adding harness runtime commands or database state.

## Codex Lifecycle Hooks

The plugin bundles Codex command hooks at `plugins/codex-project-harness/hooks/hooks.json`. Codex discovers plugin-bundled hooks after the plugin is enabled, and non-managed hooks must be reviewed and trusted with `/hooks` before they run. Hooks can be disabled globally with `[features] hooks = false`.

The harness hooks are advisory lifecycle guardrails:

- `SessionStart` prints read-only project status and dispatch summary.
- `SubagentStart` reminds worker sessions of role, task, acceptance, claim, and evidence boundaries.
- `PreToolUse` warns before broad writes when scope is not confirmed, no active task exists, or the worktree is already dirty.
- `PostToolUse` summarizes git status and reminds the agent to record validation/evidence through trusted runtime commands.
- `Stop` runs `validate`, or `validate --delivery` when `HARNESS_HOOK_DELIVERY=1`.

Set `CODEX_PROJECT_HARNESS_PLUGIN_ROOT` when the plugin is installed outside the source-tree default `plugins/codex-project-harness`. Hooks are warn-only by default. `HARNESS_HOOK_STRICT=1` makes clear hook guardrail failures return nonzero, but these hooks still do not create delivery evidence and do not replace Kernel/DB constraints, controller verification, integration hardening, HMAC/session attestation, or CI.

## Task Lease Fencing

Tasks carry a monotonic `fence` value. `task claim` returns the current fence with the lease token. `task review` bumps the fence when reviewer ownership is handed off and returns the new fence. `task recover-stale` and `task release` also bump the fence so stale holders cannot use old tokens to overwrite later work.

Write commands that hold a task lease accept `--fence`. When supplied, `task start`, `task heartbeat`, `task submit`, `task complete`, `task accept`, `task block`, and `task release` validate the fence inside the write transaction and fail with `fence-stale` before committing if the holder is stale. Omitting `--fence` remains backward compatible for older clients.

## Command Idempotency

Most mutating CLI commands accept `--request-id`. The runtime writes a `command_log` row in the same transaction as the first business mutation. A retry with the same request id and same semantic arguments returns the first stdout without reapplying the mutation. A retry with the same request id but different arguments fails with `idempotency-conflict`.

`init`, `migrate`, `repair`, and `checkpoint create/import` are admin or restore operations and do not support `--request-id` in this release.

## Agent Runner Isolation

`dispatch run` defaults to the compatible `null` runner. Passing `--runner local-process` creates or reuses an agent-specific git worktree under `.ai-team/runtime/worktrees/`, runs the command there, verifies that all branch changes are inside active `--claim-file` paths, commits claimed file changes on the agent branch, and records executor-style command evidence. File edits intended for integration must be declared with `--claim-file`; active claims conflict fail closed by exact repo-relative path. `dispatch integrate` merges agent branches inside a dedicated integration worktree and reruns delivery validation before marking the run integrated, so the user's main worktree is not branch-switched.

Before merging, `dispatch integrate` requires each active agent worktree branch to have a latest verified `task_attempt`, checks that the branch head and tree still match the verified attempt, and recomputes `git diff base..branch` to ensure every changed file is covered by active file claims for that task/agent. Unverified branches, branch drift, and claim violations fail closed and write high integration findings plus `integration_attempts` audit rows.

LocalProcessRunner is not an OS sandbox and does not create real Codex sub-sessions. Unattended use still requires host or container isolation.

## Container Controller Verification

`dispatch verify-attempt --runner container` uses Docker or Podman to rerun the linked target command in a no-network container against a read-only verification worktree for the agent branch. The controller writes stdout/stderr artifacts under `.ai-team/runtime/`, records a `sandbox_executions` audit row, and links `sandbox_execution_id`, `sandbox_engine`, and `container_image` into evidence and validation rows.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch verify-attempt \
  --run-id <run-id> --task T1 --runner container --container-image python:3.12-slim
```

Container image precedence is CLI `--container-image`, then `.ai-team/control/container-image.txt`, then `python:3.12-slim`. If Docker/Podman is unavailable, a requested container verification fails closed with `sandbox-unavailable`; it does not silently fall back to local execution.

## Native Codex Fan-Out

Harness maps to Codex native primitives instead of inventing a session protocol. `agents install` writes validated `.codex/agents/*.toml` files from the plugin templates. `dispatch export-csv <run-id>` writes native `spawn_agents_on_csv` inputs: `input.csv`, `instruction.md`, `output_schema.json`, and `spawn_config.json`. The host or user then runs Codex fan-out externally. `dispatch import-csv <run-id> --result <output.csv>` consumes native output rows as raw agent reports only; it does not trust worker self-reported command evidence. Run `dispatch verify-attempt --run-id <run-id> --task <task-id>` to have the controller re-execute the linked test target on the reported branch and produce delivery-eligible evidence. Finish with `dispatch integrate --run-id <run-id>` to merge verified branches through the integration gate.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . agents install
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch plan --scope "Feature slice"
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch native-export <run-id>
# The host creates a visible native task/thread/subagent/worktree and returns receipt.json.
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch native-import <run-id> --receipt receipt.json
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch verify-attempt --run-id <run-id> --task T1

# CSV remains a controller-mediated exchange compatibility format.
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch export-csv <run-id>
# Host/user invokes Codex spawn_agents_on_csv using .ai-team/runtime/codex-fanout/<run-id>/spawn_config.json.
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch import-csv <run-id> --result .ai-team/runtime/codex-fanout/<run-id>/output.csv
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch verify-attempt --run-id <run-id> --task T1
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch integrate --run-id <run-id>
```

## AgentProvider Lifecycle

Native Codex/ChatGPT is not an `AgentProvider` process lifecycle. `dispatch native-export` writes immutable task packages under `.ai-team/runtime/native-dispatch/<run-id>/` without copying SQLite state or choosing a model slug. The host owns visible task/thread/subagent/worktree, approval, sandbox, model, cancel, steer, and handoff. `dispatch native-import` accepts a receipt only when its package hash, Kafa assignment, current constraints, branch, base/head SHA, required policy metadata, and real non-placeholder host IDs match. It records those IDs plus a raw `agent_report/task_attempt`; it creates no evidence. `dispatch verify-attempt` remains the controller trust boundary.

Native worktrees are recorded as `host-managed`. Kafa may integrate their branch but does not remove the host-owned checkout. Mutable `.ai-team/state/harness.db` remains a single-writer root-workspace fact source; managed worktrees receive package facts, not copied databases. Hosted/cloud tasks without a separately authenticated Project Fact Transport are unsupported for Kernel mutation.

`dispatch provider start` records legacy Host SDK or fixture-managed agent sessions for ready dispatch assignments. CSV is not a provider; use `dispatch export-csv/import-csv` only as controller-mediated exchange. `dispatch provider collect` imports provider output as raw `agent_reports` and `task_attempts`; it never writes delivery-eligible evidence. `dispatch provider cancel` and `dispatch provider reconcile` make cancellation and timeout recovery auditable without allowing stale reports to overwrite newer work. Real native Codex session creation remains a host capability; provider lifecycle state is still a raw-report control plane, not a delivery trust anchor.

`--provider host-codex` now uses a nonblocking two-phase start. A short transaction registers `agent_provider_sessions(status='spawning')`, the corresponding `agent_sessions`, assignment claim, lease, and provider session id. The Codex worker process is spawned outside the SQLite write transaction. A second short transaction uses session id, provider session id, fence, and `status='spawning'` as a CAS guard before marking the session `running` or `spawn_failed`; cancelled or timed-out sessions are not overwritten.

The Host Codex background worker uses the optional `kafa[host-codex]` extra, which installs `openai-codex>=0.1.0b3`; the base installer and Kernel remain stdlib-only. This legacy bridge cannot inherit the native parent task permission model and therefore fails closed unless `HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all` explicitly accepts its fixed `Sandbox.workspace_write` and `ApprovalMode.deny_all` boundary. Before spawning the worker, Harness creates an assignment-specific git worktree under `.ai-team/runtime/worktrees/<run>/<task>/<agent>` and persists that path in `agent_provider_sessions.worktree_path` plus `dispatch_worktrees.worktree_path`. When the SDK turn finishes, the worker commits non-`.ai-team/` changes from the isolated worktree to the assignment agent branch, writes `.ai-team/runtime/host-codex/<run-id>/<task-id>.json`, and `dispatch provider collect` imports only the final JSON as a raw provider report. Use `dispatch verify-attempt` to produce trusted controller evidence before integration or delivery.

Each legacy worker has an independent watchdog process. `HARNESS_CODEX_TURN_TIMEOUT_SECONDS` is an enforced deadline rather than metadata, and `collect` checks both deadline and worker liveness before refreshing a lease. Cancel/timeout freezes and kills the known process tree, including detached children that remain descendants. A helper that has already reparented cannot be independently proven dead across all supported hosts, so legacy cancel/timeout always ends as `verification_failed`, preserves the assignment from automatic replan, and creates no report/evidence. Native host cancellation is the only trusted lifecycle confirmation.

Native Codex model selection is host-owned. The legacy Host Codex Provider keeps an explicit compatibility policy only: `HARNESS_CODEX_MODEL` is the highest-priority hard override; without that override, `HARNESS_CODEX_MODEL_POLICY=spark-deterministic` requires an explicit `HARNESS_CODEX_SPARK_MODEL` and selects it only for eligible low-risk developer assignments. Kafa ships no default preview model slug. Selection metadata remains raw provider metadata and does not change evidence trust, controller verification, integration, or delivery gate behavior.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch export-csv <run-id>
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch import-csv <run-id> --result <output.csv>
```

## Agent E2E Evaluation

`run_agent_e2e_eval.py --mode fixture` is the deterministic control-plane regression. It creates temporary Git repositories, calls the real CLI, writes SQLite/worktree/attempt/evidence/integration audit state, and reports JSON metrics for five deterministic scenarios: parallel success, dependency blocking, same-file claim conflict, forged evidence blocking, and integration regression blocking.

```bash
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode fixture
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode stability
```

`run_agent_e2e_eval.py --mode stability` is the CI release gate. It includes the fixture scenarios plus fake Host Codex SDK E2E, fake Host Codex Spark policy E2E, multi-role session lifecycle, connector mock server E2E, crash/retry recovery, and SQLite contention stress. The stability threshold requires zero failed scenarios, zero false passes, at least one forged evidence block, zero SQLite lock leaks, and zero unexplained human intervention.

`run_agent_e2e_eval.py --mode live-codex` is an opt-in host-environment profile for real Codex capability checks. It only attempts live work when `HARNESS_E2E_ENABLE_LIVE_CODEX=1` and the local Codex CLI/App Server is available. Otherwise it returns success with `live_skipped=true` and explicit skip reasons; skipped live mode is not evidence that real Codex E2E passed. `--mode live-command` remains a dogfood fallback using `CODEX_AGENT_EVAL_CMD`.

`run_skill_eval.py` remains a transcript marker check. It is useful for format drift, but it is not an Agent capability evaluation.

A stable example of the JSON output shape is stored at `docs/runtime/agent-e2e-eval-example.json`; real run durations are intentionally not committed.

## Connector Namespace, Resilience, And Fallback

Real connector adapters remain workflow synchronization only. `adapter confirm` can execute GitHub, Linear, Notion, Figma, and Slack operations when `payload_json.execute` is true, but connector results do not create delivery-eligible evidence and do not satisfy controller verification, HMAC/session attestation, integration, or delivery gates.

Project-level connector namespace state uses `project.connector_project_key` to identify the local project and `connector_profiles` to bind that project to existing external targets. Harness does not create Notion workspaces, Linear workspaces/projects, Slack workspaces/channels, Figma files, or GitHub repositories.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile status --json
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile set \
  --project-key my-project \
  --github-repo owner/repo \
  --linear-team TEAM \
  --linear-project PROJECT \
  --notion-parent PAGE_ID \
  --slack-channel C123456 \
  --figma-file FILE_KEY
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile unset --tool slack
```

For `payload_json.execute=true`, `adapter confirm` validates the action against the current connector profile before any external request. GitHub writes must match the bound repo, Linear writes must match the bound team or project, Notion page creates must match the bound parent page, Figma comments must match the bound file, and Slack posts must match the bound channel. Missing profiles and scope mismatches fail closed before remote API calls. `payload_json.scope_override=true` is allowed only in `write-confirm` mode, is never allowed in `write-auto`, and leaves an audit finding/event.

Connector health is recorded in `connector_budgets`, local second-level fallback artifacts in `advisory_fallbacks`, and transactional outbox state on `adapter_actions`: `execution_fence`, `claimed_at`, `claim_expires_at`, `last_recovery_at`, and `remote_recovery_count`. GitHub `gh api` calls and HTTP connectors share retry-aware handling for 429/529, common 5xx failures, GitHub rate-limit stderr/header signals, and `Retry-After`. Notion calls are throttled toward 2 req/s, Slack posting is throttled per channel, and Figma plan/tier headers are recorded as free-plan risk when present.

Before external writes, `adapter confirm` claims the action with a short SQLite transaction and fence, performs the remote write outside the transaction, and uses the same fence to mark the action completed. Concurrent confirms that do not own the claim do not call the external API. If a transport failure makes the remote result unknown, the action is marked `unknown`; subsequent confirm/reconcile must search for both stable markers, `codex-project-harness:project-key=<project_key>` and `codex-project-harness:idempotency-key=<key>`, and reuse a matching external object before any retry. Old single-marker objects are audit candidates only; they are not automatically recovered across project boundaries. When retries are exhausted or a payload is unsafe, the action is marked `blocked`, a connector finding is written, and the local `.ai-team/` fact source remains the fallback for continuing verified code delivery.

When a connector action is blocked, the Advisory Fallback Layer writes a local Markdown artifact under `docs/harness/advisory-fallbacks/` and a projection at `.ai-team/control/advisory-fallbacks.md`. GitHub fallbacks are PR/issue/comment drafts, Linear fallbacks are task and risk breakdowns, Notion fallbacks are structured spec/ADR/handoff drafts, Figma fallbacks are Product Design briefs and visual QA checklists, and Slack fallbacks are post-ready handoff summaries. Each row is explicitly `delivery_eligible=0`; these artifacts help people continue work but cannot satisfy evidence, validation, HMAC/session attestation, integration, or delivery gates.

## Target Profiles And Structured Test Semantics

Target execution policy is recorded on `test_targets`: `stack_profile`, `container_image`, `requires_sandbox`, `requires_no_network`, `result_format`, and `result_path`. `dispatch verify-attempt --runner container` resolves images by CLI override, target image, control file, then stack default. The container runner mounts source at `/src:ro`, copies it to writable `/workspace`, keeps `--network none`, and persists stdout plus structured result artifacts under `.ai-team/runtime`.

`result_format=regex` remains the compatibility mode for low/medium confidence command evidence. Structured formats (`junit`, `pytest-json`, `jest-json`, `go-json`, `cargo-nextest-json`, `playwright-json`) must parse successfully, report more than zero tests, and have `semantic_status=pass` before controller verification writes trusted pass evidence. Targets that require sandbox or no-network cannot be satisfied by the local runner or unavailable containers.

## Feature Expansion Freeze

The Phase 0 freeze protects public CLI compatibility, schema migration compatibility, delivery/trust invariants, plugin manifest and Hook/Skill surfaces, runtime scripts, and the release installation contract. Internal `core/*.py` filenames are not a product surface: deep modules may be added, split, or renamed when behavior and public contracts remain covered. The current storage baseline is schema 29; changing tables, runtime states, commands, Skills, Hooks, schemas, or trust rules still requires an explicit contract update and migration evidence.

## Installation And Release Helper

`kafa` is a root-level packaging helper, not a runtime state machine. Install it locally with:

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

Repo-scope install writes `.agents/plugins/marketplace.json` with a local `codex-project-harness` plugin entry. User-scope install copies the plugin to `~/.agents/plugins/codex-project-harness` and writes `~/.agents/plugins/marketplace.json`. Upgrade and uninstall use the same marketplace files:

```bash
kafa plugin upgrade --repo .
kafa plugin uninstall --repo .
```

`kafa` does not write harness DB rows, does not add harness runtime CLI commands, does not publish PyPI packages, and does not directly mutate Codex plugin caches.

Connector profiles are not installation state. After installing the plugin, configure external write boundaries inside each business project with `harness.py --root <project> connector profile set ...`; `kafa doctor` only reports that installation does not create external workspaces.

## Session Attestation And Independent QA

Independent QA is session-aware. `session attest` records an `agent_sessions` row plus a `session_attestations` row. Connector-origin attestations reuse the host-controlled connector HMAC key and validate the payload `agent-session:{session_id}:{agent_id}:{role}:{context_id}`. Without a key, connector-origin session attestations are recorded as manual and cannot satisfy high-trust independent QA.

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . session attest \
  --session-id S-dev --agent developer --role developer --context-id ctx-dev
python3 plugins/codex-project-harness/scripts/harness.py --root . task submit T1 \
  --agent developer --lease-token "<token>" --expected-revision 4 --fence "<fence>" \
  --evidence "implemented" --session-id S-dev
python3 plugins/codex-project-harness/scripts/harness.py --root . session attest \
  --session-id S-qa --agent qa-reviewer --role qa-reviewer --context-id ctx-qa --origin connector
python3 plugins/codex-project-harness/scripts/harness.py --root . gate record \
  --reviewer-context fresh --result pass --reviewer-session-id S-qa \
  --reviewer-attestation-id <session-attestation-id>
```

`task review` and `task accept` reject reuse of the producer session with `review-session-not-independent`. Reusing a role string with the same session id is still the same session and remains invalid for independent review. Session attestation proves that the host confirmed an independent context or session; it does not prove model reasoning quality. Reasoning quality and delivery eligibility still come from controller verification, quality gate review, and delivery gate checks.

## Fail-Closed Evidence Identity

Delivery gates require a current code identity. Git projects use the committed HEAD plus tracked source-tree hash. No-git projects must explicitly opt into content-hash evidence when recording executor output:

```bash
harness.py --root . dispatch run --agent developer --target UNIT --command "pytest" --code-identity content-hash
```

The gate rejects empty source hashes, stale source hashes, missing artifacts, empty artifacts, and artifact bytes whose SHA-256 does not match the stored `stdout_sha256`.

High and critical failure-mode coverage requires a real external trust anchor and a connector(HMAC) reviewer session attestation on the latest passing quality gate. `adapter ci-verify`, `adapter external-session-verify`, and `session attest` records with `origin=manual` are audit-only for high-risk gates. Connector-origin records must pass HMAC verification against a connector key controlled by the host or connector boundary and must match their bound payload.

The trust ladder is:

- `local-only`: local executor evidence from the current model session; eligible for low/medium risk.
- `human-confirmed`: explicit human confirmation; eligible for low/medium risk.
- `connector(HMAC)`: CI, external-session, or reviewer session attestation whose token is HMAC-SHA256 over the verification payload using `HARNESS_CONNECTOR_KEY` or the file referenced by `.ai-team/control/connector-key-path.txt`; required for high/critical risk unless the risk is formally accepted/exempt.

Recommended connector key placement is `.ai-team/runtime/connector.key`, referenced by `.ai-team/control/connector-key-path.txt`. The key itself must not be written to SQLite, event payloads, Markdown projections, or Git. `harness doctor` reports an error if the configured key file is tracked by Git.

## Unified CLI

Use:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . init
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
python3 plugins/codex-project-harness/scripts/harness.py --root . repair
python3 plugins/codex-project-harness/scripts/harness.py --root . repair --dry-run
python3 plugins/codex-project-harness/scripts/harness.py --root . migrate --from-version 6 --to-version 22
python3 plugins/codex-project-harness/scripts/harness.py --root . trace validate
python3 plugins/codex-project-harness/scripts/harness.py --root . invariant validate
python3 plugins/codex-project-harness/scripts/harness.py --root . projection rebuild
python3 plugins/codex-project-harness/scripts/harness.py --root . kernel doctor
```

When the plugin is installed outside the target project, use the proxy CLI inside the `project-runtime` skill:

```bash
python3 <project-runtime-skill-dir>/scripts/harness.py --root . status
```

## Project Bootstrap

`harness init` creates:

- `.ai-team/state/harness.db`
- generated `.ai-team/` and `docs/harness/` views
- `.codex/agents/*.toml` from plugin templates
- project metadata with `schema_version`, `runtime_version`, `project_id`, and `revision`

`harness doctor` checks required state, generated views, runtime Git hygiene, and DB rows against the machine-readable schema contracts.

`harness kernel doctor` runs the regular doctor plus Kernel v3 invariant checks through the core API.

`harness invariant validate` runs the invariant checker directly.

`harness projection rebuild` regenerates all Markdown views from SQLite through the core projection module.

`harness repair` recreates missing runtime state and views without deleting existing project files. Use `harness repair --dry-run` to see the planned repair actions without writing state. Targeted invariant repair requires explicit confirmation, for example `harness repair --clear-invariant expired-lease --confirm expired-lease`.

`harness migrate` records schema migrations and updates runtime metadata. Markdown v1 migration supports `--dry-run` and writes `docs/harness/migration-report.md` on real migration.

## State Machine

Project phase transitions are constrained:

```text
intake -> project_bootstrap -> requirement_baseline -> confirmation
confirmation -> team_architecture -> planning
confirmation -> planning
planning -> implementation -> qa -> delivery_readiness -> retrospective -> archived
qa -> implementation
```

Illegal jumps fail closed, for example:

```text
intake -> delivery_readiness
qa -> requirement_baseline
```

Use:

```bash
harness.py --root . phase project_bootstrap
```

## Task Scheduler

Tasks are stored in SQLite with:

```text
id
task
owner
status
acceptance_ids
failure_mode_ids
dependencies
lease_agent
lease_token
lease_heartbeat_at
lease_expires_at
retry_count
retry_budget
revision
evidence
```

Supported task lifecycle:

```text
ready -> claimed -> in_progress -> submitted -> review -> accepted
ready -> blocked
in_progress -> blocked
ready/in_progress -> failed
```

Key commands:

```bash
harness.py --root . task add --id T1 --task "Implement API" --acceptance AC1
harness.py --root . task next
harness.py --root . task claim T1 --agent developer --expected-revision 1
harness.py --root . task start T1 --agent developer --lease-token <token> --expected-revision 2 --fence <fence>
harness.py --root . task heartbeat T1 --agent developer --lease-token <token> --expected-revision 3 --fence <fence>
harness.py --root . task submit T1 --agent developer --lease-token <token> --expected-revision 4 --fence <fence> --evidence "tests passed"
harness.py --root . task review T1 --agent qa-reviewer --expected-revision 5
harness.py --root . task accept T1 --agent qa-reviewer --lease-token <review-token> --expected-revision 6 --fence <review-fence> --evidence "review passed"
harness.py --root . task block T1 --reason "waiting for schema decision"
harness.py --root . task release T1 --agent developer
harness.py --root . task recover-stale
```

Scheduler rules:

- Duplicate task IDs are rejected.
- Missing dependencies are rejected.
- Dependency cycles are rejected.
- `task next` returns only ready tasks whose dependencies are accepted.
- `task claim` requires expected revision and creates a lease.
- `task heartbeat` extends a valid lease and advances task revision.
- expired leases fail closed until `task recover-stale` clears them.
- `task claim` and `task start` fail when dependencies are not accepted.
- Producers submit work; reviewers accept it. `task complete` is retained as a compatibility alias for submit and does not accept work.
- stale claims fail with a revision mismatch.

## Agent Registry

Initialization installs agent templates into:

```text
.codex/agents/
```

The runtime records agent rows with role, template path, status, session ID, tool permissions, and current task lease.

The runtime does not create user-visible Codex threads by itself. It provides the local registry, lease mechanism, and local dispatch protocol that agent-capable clients can use.

Local dispatcher commands:

```bash
harness.py --root . agent capability add --agent developer --capability frontend
harness.py --root . dispatch plan --scope "Build profile UI and API"
harness.py --root . dispatch claim-next --agent developer
harness.py --root . dispatch run --agent developer --command "pytest"
harness.py --root . dispatch recover-stale
harness.py --root . dispatch status
```

Dispatcher records are local and verifiable. If the Codex host exposes true subagents, the skill should use the host mechanism and write back dispatch evidence. Otherwise it records the plan and work ownership locally.

## Event Log

Events are stored in SQLite with an autoincrement sequence:

```text
sequence
id
schema_version
type
source
target
correlation_id
causation_id
idempotency_key
payload_json
created_at
```

Events include entity type, entity id, before/after snapshots for key state changes, actor or agent when available, command context, revision/status movement, and correlation id.

Checkpoint export/import is the supported restore path. Events remain audit records and are validated for completeness, but the public runtime does not expose event replay as a recovery guarantee.

```bash
harness.py --root . checkpoint create --label before-delivery
harness.py --root . checkpoint list
harness.py --root . checkpoint export --out checkpoint.json
harness.py --root . checkpoint import --file checkpoint.json --dry-run
harness.py --root . checkpoint import --file checkpoint.json --apply
harness.py --root . event export --out events.jsonl
harness.py --root . event validate
```

Legacy JSONL events may still exist for older scripts, but the SQLite events table is the runtime event log.

## Failure Modes

Failure modes are linked to acceptance criteria and tasks:

```bash
harness.py --root . failure-mode add \
  --id FM1 \
  --feature "Profile CRUD" \
  --scenario "Duplicate submit" \
  --trigger "same request twice" \
  --expected "single durable write" \
  --risk critical \
  --acceptance AC1
```

High and critical failure modes must be covered by passing validation or formally accepted before delivery readiness can pass.

For high and critical risks, `covered` is not a self-attested flag and is not an allowed stored failure-mode status. Delivery readiness requires at least one passing validation explicitly linked with `--failure-mode FMx`, unless the risk is accepted or exempted with accepted-by, acceptance-reason, acceptance-scope, accepted-revision, and expires-at.

`failure_modes.status` records risk disposition only: `identified`, `accepted`, or `exempt`. The generated Failure Modes view includes `Derived Coverage`, which is computed from passing validation records.

Accepted risks can record:

- accepted by
- acceptance reason
- acceptance scope
- accepted project revision
- expiration date

Accepted risks expire. Expired accepted/exempt high and critical risks block delivery readiness, and can be explicitly swept back to open identified risks:

```bash
harness.py --root . risk sweep-expired
```

## Requirements, Evidence, And Findings

Requirement baselines are structured records, not prose-only notes:

```bash
harness.py --root . requirement add \
  --id R1 \
  --kind functional \
  --body "User can create a profile" \
  --priority must
```

Confirmation and team architecture require at least one requirement baseline record and at least one acceptance criterion. Planning also requires confirmed scope and a current frozen baseline.

```bash
harness.py --root . scope confirm --by project-manager --summary "User confirmed API-only scope"
harness.py --root . baseline freeze --id B1 --summary "Confirmed API-only baseline"
harness.py --root . baseline diff --from B1 --to current
harness.py --root . baseline validate
```

Traceability links requirements to acceptance criteria:

```bash
harness.py --root . requirement link --requirement R1 --acceptance AC1
harness.py --root . trace show --requirement R1
harness.py --root . trace validate
```

When a requirement baseline exists, delivery readiness requires a current frozen baseline and a complete requirement -> acceptance -> task -> passing validation chain.

Evidence, test records, and findings are also structured:

```bash
harness.py --root . test-target add --id NPM_TEST --kind unit --command-template "npm test"
harness.py --root . dispatch run --agent developer --target NPM_TEST --command "npm test"
harness.py --root . test record --id TEST1 --surface "Profile CRUD" --command "npm test" --result pass --evidence <executor-evidence-id>
harness.py --root . finding record --id F1 --surface "Profile CRUD" --severity medium --status open --summary "Needs follow-up"
```

Validation records also capture `head_commit`, `source_tree_hash`, `tracked_diff_hash`, `project_revision`, command, target, executed count, exit code, stdout hash, artifact path, trust anchor, sandbox profile, and executor policy fields. Delivery readiness fails if a passing validation was recorded against an older code snapshot, lacks a gateable registered target, does not match the target command template, has `executed_count=0`, or was not parsed from executor output.

Each active acceptance must have passing validation linked to at least one passing test or evidence item:

```bash
harness.py --root . validation record \
  --surface "Profile CRUD" \
  --acceptance AC1 \
  --failure-mode FM1 \
  --commands "npm test" \
  --findings "passed" \
  --result pass \
  --test TEST1 \
  --evidence <executor-evidence-id> \
  --target NPM_TEST \
  --trust-anchor external-session \
  --trust-anchor-id <session-id>
```

`dispatch run` uses LocalExecutor policy before starting a process:

```bash
harness.py --root . executor allow-prefix add --prefix "npm test" --reason "project test runner"
harness.py --root . dispatch run --agent developer --target NPM_TEST --command "npm test" --sandbox-profile none
harness.py --root . dispatch run --agent developer --command "custom check" --allow-unlisted --reason "one-off diagnostic"
harness.py --root . dispatch run --agent developer --runner local-process --claim-file src/app.py --command "npm test" --allow-unlisted --reason "isolated agent run"
harness.py --root . dispatch integrate --run-id <run-id>
```

`--no-network` is retained as a compatibility alias for `--sandbox-profile no-network`. In the local runtime, `no-network` records intent as `sandbox_status=unavailable`; it is not treated as OS-level isolation.

Trust anchors define what risk level evidence can satisfy:

- `local-only` and `human-confirmed` can satisfy low/medium delivery evidence.
- `external-session` and `ci` can satisfy high/critical failure-mode coverage only when their verification row is connector-origin and HMAC-valid.
- `ci` must reference a local `adapter ci-verify` record whose conclusion is `success`, whose commit SHA matches current HEAD, and whose connector token validates with the host-controlled key.
- `external-session` must reference a local `adapter external-session-verify` record whose conclusion is `verified`, whose commit SHA matches current HEAD, and whose connector token validates with the host-controlled key.

When a requirement, acceptance criterion, or failure mode changes, dependent validations and quality gates are invalidated until fresh validation or gate records resolve them.

## Quality Gates

Quality gates are fail-closed. Delivery readiness requires:

- latest gate result is `pass`
- no blocking findings
- validation records are `pass`
- no unresolved invalidations remain
- high/critical failure modes are covered by passing validation with HMAC-valid connector `ci` or `external-session` trust anchor, or formally accepted
- active tasks are accepted
- Git worktree is clean outside harness runtime files when Git exists
- gate source tree hash matches current code outside harness runtime files when Git exists

`same-context-degraded` is blocked for high/critical risk delivery.

Use the explicit delivery gate before handoff:

```bash
harness.py --root . validate --delivery
```

Moving the project into `delivery_readiness` calls the same delivery gate and fails when the gate is not satisfied.

Use:

```bash
harness.py --root . gate record \
  --reviewer-context same-context-degraded \
  --result pass \
  --commands "npm test" \
  --evidence "QA reviewed acceptance and failure modes" \
  --finding F1
```

This local form is intentionally degraded. A `fresh` gate must use the
session-aware form above with both `--reviewer-session-id` and
`--reviewer-attestation-id`; storage-level delivery validation repeats that
identity check.

## Adapter Records

External tools are recorded in SQLite adapter rows:

```bash
harness.py --root . adapter record \
  --tool github \
  --mode read-only \
  --artifact Tasks \
  --external-id issue-1 \
  --idempotency-key codex-project-harness:project:task:T1
```

Supported modes:

```text
disabled
read-only
draft-write
write-confirm
write-auto
```

Adapter actions model external work before and after the Codex host or connector performs it:

```bash
harness.py --root . adapter plan --tool github --mode write-confirm --artifact "Issue R1" --action "create issue" \
  --payload-json '{"execute":true,"operation":"github.issue.create","params":{"repo":"owner/repo","title":"R1","body":"Requirement body"}}'
harness.py --root . adapter draft --id <action-id>
harness.py --root . adapter confirm --id <action-id>
harness.py --root . adapter complete --id <action-id> --external-id GH-1 --external-link https://example.invalid/GH-1
harness.py --root . adapter reconcile
```

When `payload_json.execute` is `true`, `adapter confirm` can execute a real connector action. GitHub uses `gh api`; Linear uses `LINEAR_API_KEY`; Notion uses `NOTION_TOKEN`; Figma uses `FIGMA_TOKEN`; Slack uses `SLACK_BOT_TOKEN`. Connector tokens are read from the environment only and are not written to SQLite, events, Markdown, or logs. `unknown` connector actions are not successes; they require marker recovery or fail closed before another write is attempted.

External tools remain adapters. Local SQLite state is still sufficient for code delivery. Connector results are workflow synchronization records, not trusted delivery evidence.

## Delivery

Delivery records store scope, acceptance mapping, changed files, validation, QA, failure-mode coverage, quality gate, data/config notes, collaboration links, known gaps, and handoff notes.

`delivery record` only writes in `delivery_readiness` or `retrospective`.

Use:

```bash
harness.py --root . delivery record \
  --scope "Profile CRUD" \
  --acceptance AC1 \
  --validation "tests passed" \
  --qa "quality gate passed" \
  --failure-mode-coverage "FM1 covered" \
  --quality-gate "independent_qa pass"
```

## Verification

Runtime behavior is covered by:

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 -m py_compile plugins/codex-project-harness/scripts/*.py plugins/codex-project-harness/core/*.py plugins/codex-project-harness/hooks/*.py plugins/codex-project-harness/skills/project-runtime/scripts/harness.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 plugins/codex-project-harness/scripts/run_runtime_smoke.py
python3 plugins/codex-project-harness/scripts/run_forward_eval.py
python3 plugins/codex-project-harness/scripts/run_skill_eval.py
```

GitHub Actions runs structure checks, JSON checks, Python compilation, runtime tests, runtime smoke, forward wrapper, local skill eval, Agent E2E fixture eval, and a Kernel diagnostic smoke on push and pull request.

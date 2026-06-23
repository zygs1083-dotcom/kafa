# Codex OS Runtime Layer v3.4.1

This document describes the executable runtime layer for Codex Project Harness. The runtime turns the Harness methodology into a local project control plane for verified code delivery.

The runtime stops at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are out of scope.

Kernel v3.4.1 is an architecture generation for runtime consistency, semantic evidence, external trust anchors, safer local execution, task lease fencing, and command idempotency. The repository release remains a beta release, while the runtime implementation version is `3.4.1` and the database schema version is `16`.

## Fact Source

The primary fact source is SQLite:

```text
.ai-team/state/harness.db
```

Markdown files under `.ai-team/` and `docs/harness/` are generated human-readable views. They are useful for review and handoff, but the SQLite database is the canonical runtime source for scheduler, state, gates, events, agents, and adapters.

SQLite runs with WAL mode, foreign keys, unique constraints, task revisions, and task leases.

## Kernel v3.4.1

The executable runtime is organized around `plugins/codex-project-harness/core/`:

- `api.py` is the write facade used by the CLI and compatibility wrappers.
- `scheduler.py` owns dependency resolution, ready queues, and cycle checks.
- `lock_manager.py` owns task revision and lease validation.
- `gate_engine.py` owns delivery readiness and delivery record barriers.
- `schema_guard.py` performs pre-write entity validation and reuses row-level schema checks.
- `event_bus.py` emits, stores, validates, and dispatches audit events.
- `executor.py` runs local commands through target/prefix policy and writes trusted command evidence artifacts.
- `invariant_checker.py` verifies constraints that must not be bypassed by manual DB edits, using directed checks in write transactions and full checks for doctor/audit.
- `projections.py` is the only Markdown projection writer.

SQLite state tables remain the primary runtime fact source. Events are audit support, not the primary source of truth. Checkpoint snapshot export/import is the supported restore path.

## Task Lease Fencing

Tasks carry a monotonic `fence` value. `task claim` returns the current fence with the lease token. `task review` bumps the fence when reviewer ownership is handed off and returns the new fence. `task recover-stale` and `task release` also bump the fence so stale holders cannot use old tokens to overwrite later work.

Write commands that hold a task lease accept `--fence`. When supplied, `task start`, `task heartbeat`, `task submit`, `task complete`, `task accept`, `task block`, and `task release` validate the fence inside the write transaction and fail with `fence-stale` before committing if the holder is stale. Omitting `--fence` remains backward compatible for older clients.

## Command Idempotency

Most mutating CLI commands accept `--request-id`. The runtime writes a `command_log` row in the same transaction as the first business mutation. A retry with the same request id and same semantic arguments returns the first stdout without reapplying the mutation. A retry with the same request id but different arguments fails with `idempotency-conflict`.

`init`, `migrate`, `repair`, and `checkpoint create/import` are admin or restore operations and do not support `--request-id` in this release.

## Fail-Closed Evidence Identity

Delivery gates require a current code identity. Git projects use the committed HEAD plus tracked source-tree hash. No-git projects must explicitly opt into content-hash evidence when recording executor output:

```bash
harness.py --root . dispatch run --agent developer --target UNIT --command "pytest" --code-identity content-hash
```

The gate rejects empty source hashes, stale source hashes, missing artifacts, empty artifacts, and artifact bytes whose SHA-256 does not match the stored `stdout_sha256`.

High and critical failure-mode coverage requires a real external trust anchor. `adapter ci-verify` and `adapter external-session-verify` records with `origin=manual` are audit-only for high-risk gates. Connector-origin records must pass HMAC verification against a connector key controlled by the host or connector boundary and must match the current commit SHA.

The trust ladder is:

- `local-only`: local executor evidence from the current model session; eligible for low/medium risk.
- `human-confirmed`: explicit human confirmation; eligible for low/medium risk.
- `connector(HMAC)`: CI or external-session verification whose token is HMAC-SHA256 over the verification payload using `HARNESS_CONNECTOR_KEY` or the file referenced by `.ai-team/control/connector-key-path.txt`; required for high/critical risk unless the risk is formally accepted/exempt.

Recommended connector key placement is `.ai-team/runtime/connector.key`, referenced by `.ai-team/control/connector-key-path.txt`. The key itself must not be written to SQLite, event payloads, Markdown projections, or Git. `harness doctor` reports an error if the configured key file is tracked by Git.

## Unified CLI

Use:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . init
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
python3 plugins/codex-project-harness/scripts/harness.py --root . repair
python3 plugins/codex-project-harness/scripts/harness.py --root . repair --dry-run
python3 plugins/codex-project-harness/scripts/harness.py --root . migrate --from-version 6 --to-version 16
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
  --reviewer-context fresh \
  --result pass \
  --commands "npm test" \
  --evidence "QA reviewed acceptance and failure modes" \
  --finding F1
```

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
harness.py --root . adapter plan --tool github --mode write-confirm --artifact "Issue R1" --action "create issue"
harness.py --root . adapter draft --id <action-id>
harness.py --root . adapter confirm --id <action-id>
harness.py --root . adapter complete --id <action-id> --external-id GH-1 --external-link https://example.invalid/GH-1
harness.py --root . adapter reconcile
```

External tools remain adapters. Local SQLite state is still sufficient for code delivery. The runtime does not import GitHub/Linear/Notion/Figma/Slack SDKs or perform direct external writes.

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
python3 -m py_compile plugins/codex-project-harness/scripts/*.py plugins/codex-project-harness/core/*.py plugins/codex-project-harness/skills/project-runtime/scripts/harness.py
python3 -m unittest tests/test_harness_runtime.py tests/test_harness_operating_system.py
python3 plugins/codex-project-harness/scripts/run_runtime_smoke.py
python3 plugins/codex-project-harness/scripts/run_forward_eval.py
python3 plugins/codex-project-harness/scripts/run_skill_eval.py
```

GitHub Actions runs structure checks, JSON checks, Python compilation, runtime tests, runtime smoke, forward wrapper, local skill eval, and a Kernel diagnostic smoke on push and pull request.

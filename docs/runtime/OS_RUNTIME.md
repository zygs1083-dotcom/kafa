# Codex OS Runtime Layer v2.1

This document describes the executable runtime layer for Codex Project Harness. The runtime turns the Harness methodology into a local project control plane for verified code delivery.

The runtime stops at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are out of scope.

## Fact Source

The primary fact source is SQLite:

```text
.ai-team/state/harness.db
```

Markdown files under `.ai-team/` and `docs/harness/` are generated human-readable views. They are useful for review and handoff, but the SQLite database is the canonical runtime source for scheduler, state, gates, events, agents, and adapters.

SQLite runs with WAL mode, foreign keys, unique constraints, task revisions, and task leases.

## Unified CLI

Use:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . init
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . repair
python3 plugins/codex-project-harness/scripts/harness.py --root . migrate --from-version 1 --to-version 2
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

`harness doctor` checks required state and generated views.

`harness repair` recreates missing runtime state and views without deleting existing project files.

`harness migrate` records schema migrations and updates runtime metadata.

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
retry_count
retry_budget
revision
evidence
```

Supported task lifecycle:

```text
ready -> claimed -> in_progress -> accepted
ready -> blocked
in_progress -> blocked
ready/in_progress -> failed
```

Key commands:

```bash
harness.py --root . task add --id T1 --task "Implement API" --acceptance AC1
harness.py --root . task next
harness.py --root . task claim T1 --agent developer --expected-revision 1
harness.py --root . task start T1 --agent developer
harness.py --root . task complete T1 --evidence "tests passed"
harness.py --root . task block T1 --reason "waiting for schema decision"
harness.py --root . task release T1 --agent developer
```

Scheduler rules:

- Duplicate task IDs are rejected.
- Missing dependencies are rejected.
- Dependency cycles are rejected.
- `task next` returns only ready tasks whose dependencies are accepted.
- `task claim` requires expected revision and creates a lease.
- stale claims fail with a revision mismatch.

## Agent Registry

Initialization installs agent templates into:

```text
.codex/agents/
```

The runtime records agent rows with role, template path, status, session ID, tool permissions, and current task lease.

The runtime does not create user-visible Codex threads by itself. It provides the local registry and lease mechanism that agent-capable clients can use.

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

This provides a replayable ordered audit trail inside `.ai-team/state/harness.db`.

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

High and critical failure modes must be `covered` or formally `accepted` before delivery readiness can pass.

Accepted risks can record:

- accepted by
- acceptance reason
- expiration date

## Quality Gates

Quality gates are fail-closed. Delivery readiness requires:

- latest gate result is `pass`
- no blocking findings
- validation records are `pass`
- high/critical failure modes are closed
- active tasks are accepted
- Git worktree is clean when Git exists
- gate commit matches current HEAD when Git exists

`same-context-degraded` is blocked for high/critical risk delivery.

Use:

```bash
harness.py --root . gate record \
  --reviewer-context fresh \
  --result pass \
  --commands "npm test" \
  --evidence "QA reviewed acceptance and failure modes"
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
off
read-only
draft-write
write-confirm
write-auto
```

External tools remain adapters. Local SQLite state is still sufficient for code delivery.

## Delivery

Delivery records store scope, acceptance mapping, changed files, validation, QA, failure-mode coverage, quality gate, data/config notes, collaboration links, known gaps, and handoff notes.

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
python3 -m unittest tests/test_harness_runtime.py tests/test_harness_operating_system.py
```

GitHub Actions runs structure checks, JSON checks, Python compilation, and runtime tests on push and pull request.

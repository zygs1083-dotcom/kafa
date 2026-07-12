# Codex Project Harness Local Runtime

Codex Project Harness is a local-only verified delivery runtime. It records the
facts needed to decide whether a local code candidate is ready for handoff. It
does not deploy, release to production, provision infrastructure, run
production data migrations, change secrets, or create paid resources.

The runtime described here uses schema 30. Release, package, and runtime version
values are maintained by the repository's version source rather than repeated
in this architecture guide.

## Authority Model

Three authorities cooperate without duplicating ownership:

| Authority | Owns | Does not own |
| --- | --- | --- |
| OpenSpec | Proposal, design, behavioral specs, implementation tasks, and change archive for substantial work. | Runtime delivery facts or candidate verification. |
| Native Codex/ChatGPT host | User-visible tasks, threads, subagents, worktrees, approval, model selection, cancellation, steering, and handoff. | Kafa SQLite state or delivery decisions. |
| Kafa local runtime | Current-cycle delivery facts, acceptance links, root-owned task state, immutable executions, validation judgments, findings, review gates, and delivery decisions. | A second host lifecycle, remote project-management synchronization, or spec duplication. |

For an OpenSpec-managed change, OpenSpec remains the specification authority.
Kafa records only the minimal local facts needed to verify the resulting
candidate. Generated Markdown is a view; it never replaces either authority.

## Local-Only Boundary

The supported business-project runtime uses only:

- the project filesystem;
- local Git identity, or deterministic content identity when Git is absent;
- per-project SQLite;
- local command execution; and
- optional local container execution with no network.

The Plugin runtime does not call GitHub, Linear, Notion, Figma, Slack, or other
project-management services. It does not request service tokens, invoke `gh
api`, run a hidden Host SDK worker, or convert a model-visible report into
delivery provenance. A user may still use external apps through the native host,
but those actions are outside Kafa runtime state and trust.

## Fact Source

The canonical runtime fact source is:

```text
.ai-team/state/harness.db
```

SQLite runs with foreign-key checks and explicit transactions. Only the root
controller writes the database. Workers and reviewers return code, findings,
commands, and context through the native host; the root controller decides what
to record.

Files under `.ai-team/` and `docs/harness/` are generated local projections for
human review. They can be rebuilt from SQLite and are never accepted as a
substitute for database facts.

Schema 30 contains exactly these 27 active tables:

```text
project
delivery_cycles
requirements
acceptance
requirement_acceptance
failure_modes
failure_mode_acceptance
baselines
tasks
task_acceptance
task_failure_modes
task_dependencies
test_targets
task_test_targets
executions
validations
validation_executions
validation_failure_modes
findings
quality_gates
quality_gate_findings
deliveries
delivery_acceptance
decisions
invalidations
migrations
events
```

Retired remote-integration, provider, worktree, report, snapshot, and global
command-log tables are not created in a fresh schema 30 database.

## Runtime Module Contracts

The executable boundary lives under
`plugins/codex-project-harness/core/`:

- `api.py` is the **explicit public API** used by the CLI.
- `schema_lifecycle.py` owns **Schema Lifecycle** initialization, migration,
  backup, and integrity checks.
- `store.py` owns concrete SQLite connection and transaction behavior.
- `cycle_ledger.py` owns the **Cycle Ledger** for current-cycle identity,
  baselines, and traceability reads.
- `execution.py` owns exact-target local/container execution and structured
  result parsing.
- `delivery.py` owns the schema 30 **Delivery Decision** and local trust policy.
- `event_bus.py` appends compact audit events.
- `projections.py` rebuilds affected generated views.
- `api.py` and `scripts/harness.py` expose only supported operations; the CLI
  parses arguments and formats results rather than reimplementing trust rules.

Compatibility helpers may remain in the isolated migration path, but they do
not expand the active schema or public runtime surface.

## Current Cycle and Candidate

Delivery facts are scoped to an active cycle and a current candidate. Old
cycles remain auditable, but their passes do not satisfy a new cycle and their
failures do not block it. A candidate change makes prior executions,
validations, and gates stale for delivery even though their rows remain
available for audit.

The runtime identifies the candidate with the current local Git revision and
worktree state, or with local content identity when Git is unavailable. A
passing review gate cannot be recorded against a dirty Git worktree. Kafa never
creates, switches, merges, or removes host-owned worktrees.

Candidate hashing uses actual runtime bytes, executable mode, fixed per-file
SHA-256 framing, and ignored local runtime files; `.gitignore` is not a trust
boundary. Git probes clear ambient `GIT_*`, disable fsmonitor and lazy fetch,
disable replace-object lookup, and fail closed on missing local objects,
symlinks, gitlinks, unmerged entries,
or non-regular paths in either the index or HEAD. Kafa-owned state, generated harness views/templates, Git
internals, and generated Python caches remain outside the candidate so
recording evidence does not invalidate itself.

Every production projection publication holds the project operation lock across
database reads and filesystem writes. Doctor verifies view content against an
independent snapshot rendering. Migration rollback quarantines failed-schema
WAL/SHM before restoring and ordinarily opening the source backup; otherwise it
retains rollback-incomplete recovery state.

The generated `project-state.yaml` timestamp is the authoritative
`project.updated_at`, not a render-time clock. Rebuild uses replace rather than
merge semantics so unchanged facts are byte-stable and stale ad-hoc keys are
removed. The projection contains exact DB schema keys (`id` and
`current_cycle_id` included) and no generic `blocked_reason`.

Core independently verifies projection content after the publication callback;
callback self-report is not trusted. Operation-lock descriptor cleanup is
`BaseException`-safe. Git identity pins the explicit root with `GIT_WORK_TREE`,
and catalog validation allows only the 27 schema-30 tables plus
`sqlite_sequence`. Real Native controller subprocesses execute from a
start-verified private Git-backed snapshot and require completion identity to
match. Callback-era DB fingerprints must remain equal, and snapshot Git init
uses an ambient-free environment plus an empty template.

## Root-Controller Task Lifecycle

Task state is intentionally small and single-writer:

```text
planned -> active -> submitted -> accepted
                    |           -> blocked
                    -> blocked
planned/active/submitted -> cancelled
```

The root controller performs every transition. There are no task leases,
heartbeats, expiry timers, fencing tokens, claim/release operations, reviewer
leases, retry budgets, or worker database writes. SQLite transactions, natural
keys, revisions, and explicit state preconditions reject illegal or duplicate
transitions.

`submitted_context_id` and reviewer context identifiers are self-reported audit
metadata. They can document procedural separation, but they are not
cryptographic identities or independent host attestations.

## Immutable Execution and Validation

Verification starts from a registered `test-target` whose exact command and
result format are local facts. The root controller then runs:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . \
  verify run --target UNIT --acceptance AC1
```

The command runs outside the SQLite write transaction. After execution, one
transaction records:

- one insert-only execution for the current cycle and candidate;
- command, exit code, parsed test count, semantic result, and target identity;
- stdout artifact path and SHA-256 digest;
- runner, sandbox, no-network, and policy status;
- one validation judgment and its execution link; and
- one compact audit event.

Execution rows cannot be updated or replaced. Validation rows contain judgment
and supersession fields; they reference execution facts instead of copying or
overriding command evidence.

A zero exit code alone is insufficient. Structured targets fail closed when the
result artifact is missing, malformed, failing, or reports zero executed tests.
Targets that require a sandbox or no-network execution fail closed when that
policy was not actually satisfied. An unavailable requested container is
reported as unavailable, not silently replaced by local execution.

Generated structured-result paths should remain under `.ai-team/runtime/` or be
emitted on stdout. Writing them to ordinary project paths changes the candidate,
so the post-execution identity check discards the completed result rather than
granting verification credit to a different source tree.

`validation record` stores audit judgment only. Free-form text, a claimed exit
code, a pasted digest, or a model-generated report cannot create a
delivery-eligible execution.

## Honest Review and High-Risk Work

Local trust labels are deliberately limited:

- `controller-verified`: the root controller executed a target on the current
  candidate;
- `reviewed-local`: producer and reviewer context metadata are distinct;
- `same-context-degraded`: a same-context review, allowed only for low or medium
  risk; and
- `human-review-required`: autonomous delivery is blocked.

High or critical work requires a structured current-candidate execution and
distinct producer/reviewer context metadata. Those local identifiers still do
not prove independent identity. Without verifiable provenance, the result is
`human-review-required`, not pass.

A user may explicitly accept or exempt each remaining high/critical risk only
with recorded actor, reason, scope, current revision, and unexpired expiry. That
path is labeled procedural accepted risk; it is never described as
cryptographic proof. Open high or critical findings continue to block delivery.

`skipped`, `blocked`, `not-run`, unavailable, and fixture-only outcomes are not
passes.

## Delivery Decision

The Delivery Decision reads only current-cycle, current-candidate facts. A
delivery record requires, at minimum:

- acceptance criteria linked to the implemented tasks;
- tasks in accepted state;
- active passing validations linked to immutable current executions;
- satisfied target sandbox/no-network and structured-result policies;
- no unresolved blocking finding;
- the latest applicable quality gate for the same candidate; and
- honest handling of every active high/critical failure mode.

Recording delivery closes the current cycle as delivered. A later cycle starts
with new candidate-scoped obligations; it does not inherit delivery credit.

## Audit Events Are Not Recovery

`events` is a compact append-only audit log. Each row records the affected
entity, root-controller actor, command, bounded before/after summaries,
correlation identifier, and timestamp. Update and delete protections preserve
append-only behavior.

Events intentionally omit whole-table snapshots, arbitrary command output,
tokens, and raw remote payloads. They are not an event-sourcing journal and
cannot reconstruct the database. The audit log exposes no public inspection or
recovery command family.

Migration and administrator recovery use consistent SQLite backups with
integrity checks and SHA-256 digests. `repair` creates and verifies a backup
before mutation. Schema 27, 28, and 29 migrations use a side-by-side schema 30
database and activate it only after schema, foreign-key, invariant, and
projection validation.

If migration fails before activation, the source database remains active and
byte-preserved. If final doctor checks fail after activation, the runtime moves
the failed schema 30 database aside and restores the verified backup. Once new
schema 30 facts exist, restoring an older backup is an explicit operator action
that warns about loss of post-migration facts and requires the expected backup
digest.

## Targeted Projections

Normal mutations rebuild only affected views. For example, a requirement change
updates project state, requirements, and traceability without rewriting
unrelated finding or delivery views. Administrator recovery can rebuild all
supported local views:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . projection rebuild
```

The runtime does not generate external tooling maps or advisory fallback views.

## Public CLI

The supported top-level runtime commands are:

```text
init, status, doctor, quickstart
cycle, requirement, acceptance, failure-mode, baseline, trace
task, test-target, verify, validation
finding, gate, delivery, decision
validate, repair, migrate, projection
```

Common entrypoints are:

```bash
kafa project doctor --repo /path/to/project
kafa project init --repo /path/to/project
kafa project status --repo /path/to/project

python3 plugins/codex-project-harness/scripts/harness.py --root . quickstart status
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
```

`kafa doctor --repo .` validates a Kafa or Plugin source repository. Use `kafa
project doctor` for an ordinary business project.

Removed v1 command families are not retained as compatibility stubs. A retired
invocation fails before network access or runtime mutation and directs the user
to the local replacement.

## Plugin Surface

The distribution contains seven delivery-focused Skills:

```text
project-harness
minimal-safe-change
bug-fix-loop
test-first-delivery
independent-quality-gate
harness-audit
project-retrospective
```

Project initialization exposes at most the `developer`, `architect`, and
`qa-reviewer` templates. Templates guide native host roles; they do not create
Kafa-owned sessions.

Exactly three default Hooks are supported:

- `SessionStart` reads local status once;
- `SubagentStart` injects root-controller, task, and evidence boundaries; and
- `Stop` gives warn-only readiness guidance.

Hooks skip uninitialized projects without creating `.ai-team`. They are
advisory and never create execution evidence, mutate delivery state, or turn a
failed readiness check into a process-blocking trust claim.

## Operational Checks

Use these checks at handoff:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . status
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
```

Report each command with its actual outcome and test count. A readiness failure
is evidence of an unmet obligation, not a reason to weaken the gate.

# Changelog

All notable repository releases are documented here.

This project now uses Git tags for release points. Earlier commits remain in Git history, but formal release tagging starts at `v0.4.0-beta.1`.

## v1.4.0-beta.1 - 2026-06-23

### Added

- Controller-side Codex fan-out verification through `dispatch verify-attempt`, which reruns linked test targets on the reported agent branch before producing trusted evidence.
- `task_attempts`, `agent_reports`, and `task_test_targets` runtime records for branch-bound attempts, raw worker reports, and per-task validation targets.
- `test-target link --task <id> --target <id>` so dispatch export no longer assigns every task the first global gateable target.
- Dispatch assignment lease expiry fields so `dispatch recover-stale` only recovers truly expired work.

### Changed

- Runtime schema version is now `19`; runtime implementation version is now `3.7.0`.
- `dispatch import-csv` now imports worker reports only; worker self-reported command evidence is not delivery-eligible.
- Dispatch planning, CSV export, and claim-next now use the dependency-aware ready queue.
- Local-process agent branches are checked against active file claims, including commits created inside the agent command.
- `dispatch integrate` uses an isolated integration worktree instead of switching the user's main worktree.
- CI now runs `python3 -m unittest discover -s tests -p 'test_*.py'` so all harness regression tests are covered.

### Boundaries

- Harness still does not spawn Codex sessions or call Codex APIs. Native fan-out execution remains host/user-provided.
- `ContainerRunner` records container/no-network intent and falls back honestly when host isolation is unavailable; it is not a production sandbox guarantee.

## v1.3.0-beta.1 - 2026-06-23

### Added

- Native Codex agent installation through `agents install`, using `.codex/agents/*.toml` templates with schema validation and no silent overwrite.
- Codex fan-out export through `dispatch export-csv`, generating `input.csv`, `instruction.md`, `output_schema.json`, and `spawn_config.json` for `spawn_agents_on_csv`.
- Codex fan-out import through `dispatch import-csv`, consuming native output CSV rows and recording trusted command evidence only when parsed evidence, branch, target, artifact hash, source hash, and task fence checks pass.

### Changed

- Runtime schema version is now `18`; runtime implementation version is now `3.6.0`.
- Codex fan-out remains optional; unavailable native subagents fall back to v1.2 `dispatch run --runner null|local-process`.

### Boundaries

- Harness does not call Codex APIs or spawn sessions. It prepares native inputs, consumes native outputs, and keeps P0/P1 consistency and delivery gates intact.

## v1.2.0-beta.1 - 2026-06-23

### Added

- AgentRunner abstraction for dispatch execution, with compatible `null` runner and explicit `local-process` runner.
- Local process dispatch can run commands in agent-specific git worktrees and record executor-style command evidence.
- File claim tracking rejects concurrent active claims for the same repo-relative path with `file-claim-conflict`.
- Dispatch integration merges agent branches into a staging `integration/<run-id>` branch and reruns delivery validation.

### Changed

- Runtime schema version is now `17`; runtime implementation version is now `3.5.0`.
- `dispatch run` supports `--runner` and repeated `--claim-file`.

### Boundaries

- LocalProcessRunner is not an OS sandbox. It does not create real Codex sub-sessions, cross-machine locks, external writes, deployment, or production release.

## v1.1.1-beta.1 - 2026-06-23

### Added

- Command-level idempotency for mutating CLI commands via `--request-id`.
- `command_log` records request id, command name, stable argument hash, first stdout, and creation time.
- Duplicate requests with the same arguments return the first stdout without reapplying the mutation.
- Duplicate request ids with different arguments fail with `idempotency-conflict`.

### Changed

- Runtime schema version is now `16`; runtime implementation version is now `3.4.1`.
- Admin/restore commands `init`, `migrate`, `repair`, and `checkpoint create/import` remain outside request-id idempotency for this release.

### Boundaries

- This release does not change task fencing, HMAC trust anchors, delivery gates, invariant logic, dispatch internals, or `core/store.py`.

## v1.1.0-beta.1 - 2026-06-23

### Added

- Store seam from T1: runtime DB access is mediated through the store abstraction and supports in-memory test stores.
- Task fencing from T2: tasks now carry a monotonic `fence` value so stale lease holders can be rejected inside the write transaction.
- `task claim` and `task review` print `fence=<n>` alongside the lease token.
- `task start`, `task heartbeat`, `task submit`, `task complete`, `task accept`, `task block`, and `task release` accept optional `--fence`.

### Changed

- `task review`, `task recover-stale`, and `task release` bump the task fence when ownership changes or stale leases are recovered.
- Runtime schema version is now `15`; runtime implementation version is now `3.4.0`.

### Boundaries

- Fencing is limited to task lease write paths in this release. Validation/evidence records, dispatch runs, HMAC trust anchors, delivery gates, idempotency, and `core/store.py` behavior are unchanged.

## v1.0.2-beta.1 - 2026-06-23

### Fixed

- Connector-origin CI and external-session anchors now require HMAC verification instead of trusting any non-empty `verification_token`.
- Without `HARNESS_CONNECTOR_KEY` or a configured connector key file, connector-origin writes are downgraded to manual audit records and cannot cover high/critical failure modes.
- Delivery gates recompute connector HMAC tokens from the verification payload, so tampered commit SHA or conclusion fields fail closed.
- `harness doctor` reports an error when the configured connector key file is tracked by Git.

### Changed

- Runtime schema version is now `14`; runtime implementation version is now `3.3.2`.
- CI and external-session verification rows include `token_status` and `token_reason` audit fields.
- Documentation now defines trust as `local-only < human-confirmed < connector(HMAC)` and states that connector key material must be controlled outside the model session.

### Boundaries

- The key itself is never written to SQLite, event payloads, Markdown projections, or Git by the runtime.
- This release still stops at verified code delivery and performs no deployment, real external writes, or connector-side polling.

## v1.0.1-beta.1 - 2026-06-23

### Fixed

- Delivery gates now fail closed when no committed code identity is available; no-git projects must explicitly use content-hash evidence.
- Passing validation/evidence must carry a non-empty current source hash, and stdout artifacts are re-hashed at gate time to detect tampering.
- Acceptance validation checks now scan all passing candidates and accept any trusted candidate instead of only inspecting the newest record.
- `external-session` anchors must reference recorded external-session verification contracts.
- CI anchors now distinguish `origin=manual` from `origin=connector`; only connector-origin records with verification tokens can cover high/critical failure modes.

### Changed

- Runtime schema version is now `13`; runtime implementation version is now `3.3.1`.
- `dispatch run`, `validation record`, and `evidence record` support explicit `--code-identity content-hash` for no-git projects.
- `adapter ci-verify` accepts optional `--origin` and `--verification-token`; `adapter external-session-verify` records independent-session verification contracts.

### Boundaries

- Manual CI/external-session records remain useful audit records, but they do not satisfy high/critical external trust gates.
- This release still stops at verified code delivery and performs no deployment or real external writes.

## v1.0.0-beta.1 - 2026-06-23

### Added

- Trust anchors for validation and evidence: `local-only`, `human-confirmed`, `external-session`, and `ci`.
- `adapter ci-verify` for local CI verification contracts with provider, run id, conclusion, commit SHA, and link.
- Test target gateability metadata so placeholder commands such as `echo` and `true` cannot satisfy delivery gates.
- Sandbox profile audit fields and mandatory allow-unlisted reasons for local executor runs.

### Changed

- Runtime schema version is now `12`; runtime implementation version is now `3.3.0`.
- Delivery gates now require passing validation command evidence to come from executor-parsed output, not manual count/hash fields.
- High and critical failure-mode coverage now requires `ci` or `external-session` trust anchors unless the risk is formally accepted.
- `--no-network` is now a compatibility alias for audited `sandbox_profile=no-network` and is recorded as unavailable in the local runtime.

### Boundaries

- This release still stops at verified code delivery.
- The runtime records CI and external-session trust contracts locally; it does not fetch CI or create real external sessions.
- External tools remain adapter contracts; no real GitHub/Linear/Notion/Figma/Slack writes are performed by the runtime.

## v0.9.0-beta.1 - 2026-06-22

### Added

- Test target registry with `test-target add/list`, projected to `.ai-team/control/test-targets.md`.
- Executor allow-prefix management and LocalExecutor command policy evidence.
- `executed_count`, target, policy, allow-unlisted, and no-network fields for evidence and validation records.
- Directed invariant checks for write transactions, with full invariant validation retained for doctor and `invariant validate`.
- Targeted `repair --clear-invariant <code> --confirm <code>` for expired leases and producer self-acceptance.

### Changed

- Runtime schema version is now `11`; runtime implementation version is now `3.2.0`.
- Delivery gates now require passing validations to reference a registered test target, match that target command, and prove `executed_count > 0`.
- LocalExecutor rejects unlisted commands by default, records rejected evidence without running the process, and marks `--no-network` as an audit/environment hint.
- Runtime smoke now includes a 5000-entity directed-invariant benchmark.

### Boundaries

- This release still stops at verified code delivery.
- `--no-network` is not an OS-level network sandbox.
- External tools remain adapter contracts; no real GitHub/Linear/Notion/Figma/Slack writes are performed by the runtime.

## v0.8.0-beta.1 - 2026-06-22

### Added

- Trusted command evidence fields for evidence and validation records: command, exit code, stdout SHA-256, artifact path, and source tree hash.
- LocalExecutor and `dispatch run` for executing local commands, writing stdout artifacts, and recording command evidence.
- Pre-commit invariant enforcement inside runtime write transactions so failed invariants roll back state changes.
- Single-source runtime enum imports for task statuses, failure-mode statuses, and adapter modes.

### Changed

- Runtime schema version is now `10`; runtime implementation version is now `3.1.0`.
- Delivery gates now require passing validations to be backed by trusted command evidence with exit code `0` and a current source tree hash.
- Event replay is no longer a public CLI promise; checkpoint export/import remains the supported snapshot restore path.

### Boundaries

- This release still stops at verified code delivery.
- LocalExecutor is a local single-machine executor, not an OS-level sandbox or distributed worker.
- External tools remain adapter contracts; no real GitHub/Linear/Notion/Figma/Slack writes are performed by the runtime.

## v0.7.0-beta.1 - 2026-06-22

### Added

- Codex Harness Kernel v3.0 core package with dedicated API, scheduler, lock manager, gate engine, schema guard, event bus, invariant checker, and projection modules.
- Kernel diagnostics through `kernel doctor`, explicit runtime invariant checks through `invariant validate`, and generated view recovery through `projection rebuild`.
- Scheduler enforcement that `task next` only returns tasks whose dependencies are accepted, with dependency resolution centralized in the core scheduler.
- Lock enforcement for task revision checks, lease owner/token validation, lease expiration, and stale recovery through the core lock manager.
- Replay-compatible event validation and replay rebuilding through the core event bus, starting from explicit checkpoints.
- Runtime invariant checks for illegal task states, stale active leases, accepted-task evidence/reviewer separation, delivery alignment, high/critical risk state, and checkpoint-era event completeness.

### Changed

- Runtime schema version is now `9`; runtime implementation version is now `3.0.0`.
- CLI and legacy wrappers now route through the `core.api` facade while preserving existing public commands.
- Delivery readiness, `validate --delivery`, and `delivery record` share the same core gate engine.
- Markdown projections are centralized in `core/projections.py`; SQLite remains the primary runtime fact source.
- Task records include `submitted_by` and `accepted_by` audit fields for invariant validation.

### Boundaries

- SQLite state tables remain the source of truth; the event bus records, validates, dispatches, and replays from checkpoints but does not replace SQLite with event sourcing.
- This release still stops at verified code delivery.
- Real external writes and real Codex sub-session creation remain host/connector capabilities, represented locally by adapter and dispatch records.

## v0.6.0-beta.1 - 2026-06-22

### Added

- Scope confirmation and frozen requirement baselines with `scope confirm`, `baseline freeze`, `baseline diff`, and `baseline validate`.
- Delivery records now require `delivery_readiness` or `retrospective`, and delivery readiness requires a current frozen baseline.
- Validation quality links through `validation_tests`, `validation_evidence`, and `quality_gate_findings`.
- Checkpoint export/import, event export/validate/replay, and replay-compatible runtime snapshots.
- Local dispatcher capability matching, dispatch runs, assignments, stale recovery, and status reporting.
- Adapter action lifecycle commands: `adapter plan`, `adapter draft`, `adapter confirm`, `adapter complete`, and `adapter reconcile`.
- `risk sweep-expired` for turning expired accepted/exempt risks back into open identified risks.
- Executable fresh skill eval fixture harness via `run_skill_eval.py`.

### Changed

- Runtime schema version is now `8`; runtime implementation version is now `2.6.0`.
- Delivery validation requires each active acceptance to have passing validation linked to a passing test or evidence.
- High/critical failure-mode coverage must come from passing validation plus linked test/evidence on the current code snapshot.

### Boundaries

- This release still stops at verified code delivery.
- Real GitHub/Linear/Notion/Figma/Slack writes remain adapter/action contracts executed by the Codex host or connector, then reconciled locally.
- Real Codex sub-session creation remains host-provided; the repository implements local dispatch protocol, fixtures, CLI, and evidence.

## v0.5.0-beta.1 - 2026-06-22

### Added

- Structured requirement-to-acceptance traceability with `requirement link`, `trace show`, `trace validate`, and generated `.ai-team/requirements/traceability.md`.
- Delivery validation now fails closed on incomplete requirement trace chains when a requirement baseline exists.
- Lightweight schema contract execution for runtime DB rows using the repository JSON schema files.
- Migration dry-run and migration report support for Markdown v1 imports.
- Repair dry-run support that reports the planned runtime recovery actions without writing state.
- Runtime Git hygiene checks for `.ai-team/state/`, `.ai-team/backups/`, and `.ai-team/runtime/`.
- Audit-rich runtime events for phase, requirement, acceptance, failure-mode, task, validation, quality-gate, and delivery state changes.
- `run_runtime_smoke.py` as the canonical executable runtime smoke script, with `run_forward_eval.py` kept as a compatibility wrapper.
- Fresh-session skill evaluation prompt documentation for future real Codex session and subagent evaluations.

### Changed

- Runtime schema version is now `7`; runtime implementation version is now `2.5.0`.
- Project initialization writes runtime `.gitignore` entries so local DB and runtime files do not become source artifacts.
- Harness-generated `.gitignore` runtime protection is excluded from source cleanliness and source hash gates.
- Markdown v1 migration imports a broader set of historical views, including requirements, validation, quality gates, deliveries, and decisions.

### Boundaries

- This release still stops at verified code delivery.
- Real GitHub/Linear/Notion/Figma/Slack external synchronization remains an adapter boundary, not a guaranteed write workflow.
- Real fresh Codex session spawning and autonomous subagent dispatch remain future evaluation targets.

## v0.4.0-beta.1 - 2026-06-22

### Added

- SQLite-backed project harness runtime with structured project, requirement, acceptance, failure-mode, task, validation, evidence, finding, quality-gate, delivery, adapter, agent, migration, and event records.
- Unified `harness.py` CLI for initialization, phase transitions, requirement/acceptance/failure-mode/task management, validation, evidence, findings, gates, delivery, adapters, doctor, repair, and migration.
- Task lifecycle with revision checks, producer/reviewer separation, leases, heartbeat, stale lease recovery, and agent lease release.
- Fail-closed delivery validation for open tasks, failed validations, stale invalidations, risk acceptance expiry, dirty Git worktrees, stale quality gates, and stale validation code snapshots.
- Failure Mode coverage derivation from passing validation records instead of manually stored `covered` status.
- Generated Markdown views for human-readable project state, task board, requirements, failure modes, validation, evidence, findings, quality gates, deliveries, decisions, and tooling map.
- Compatibility wrappers for legacy scripts, routed through the unified CLI.
- Machine-readable schemas for runtime entities, including requirements, evidence, tests, findings, invalidations, validation snapshots, failure modes, quality gates, adapters, agents, and delivery records.
- Runtime regression tests and executable forward-eval smoke scenarios.

### Changed

- The repository release version is now tracked explicitly in `VERSION`, `CHANGELOG.md`, Git tags, and the plugin manifest.
- README now distinguishes the repository release version from the Code Delivery Architecture generation.
- External collaboration tools are modeled as optional adapters; local SQLite remains the canonical runtime fact source.
- High/critical risk delivery requires passing validation linked to each relevant Failure Mode, or a scoped, non-expired risk acceptance.

### Boundaries

- This release is a controlled beta for verified code delivery.
- It does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation.
- GitHub/Linear/Notion/Figma/Slack are represented through adapter records and workflow guidance; full external write synchronization is not yet a release guarantee.

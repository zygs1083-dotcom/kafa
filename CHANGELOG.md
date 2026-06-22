# Changelog

All notable repository releases are documented here.

This project now uses Git tags for release points. Earlier commits remain in Git history, but formal release tagging starts at `v0.4.0-beta.1`.

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

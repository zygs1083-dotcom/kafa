# Changelog

All notable repository releases are documented here.

This project now uses Git tags for release points. Earlier commits remain in Git history, but formal release tagging starts at `v0.4.0-beta.1`.

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

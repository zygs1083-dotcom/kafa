# Harness Operating System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining runtime-system gaps identified in `# 总体判断.md`.

**Architecture:** Add a SQLite-backed canonical runtime with generated Markdown views, a unified CLI, constrained phase transitions, task scheduling, task leases, event sequencing, agent registry installation, adapter records, doctor/repair/migrate commands, and automated runtime/forward-eval tests.

**Tech Stack:** Python standard library, SQLite, unittest, GitHub Actions.

---

### Task 1: Runtime Regression Tests

**Files:**
- Create: `tests/test_harness_operating_system.py`

- [x] Write tests for SQLite initialization, agent installation, illegal phase transitions, dependency scheduling, task leases, doctor/repair/migrate, adapter records, delivery records, and duplicate task IDs.
- [x] Run tests and confirm they fail before the unified runtime exists.
- [x] Keep `tests/test_harness_runtime.py` passing for legacy compatibility.

### Task 2: SQLite Runtime

**Files:**
- Create: `plugins/codex-project-harness/scripts/harness_db.py`
- Create: `plugins/codex-project-harness/scripts/harness.py`

- [x] Create SQLite schema for project, acceptance, failure modes, tasks, validations, quality gates, deliveries, adapters, agents, migrations, and events.
- [x] Enable WAL mode, foreign keys, busy timeout, transactions, revisions, leases, and event sequence.
- [x] Implement constrained phase transitions.
- [x] Implement task add/update/next/claim/start/complete/block/release.
- [x] Implement doctor, repair, migrate, adapter record, delivery record, validation record, gate record.
- [x] Generate Markdown views from SQLite.

### Task 3: Compatibility And Installation

**Files:**
- Modify: `plugins/codex-project-harness/scripts/init_project_harness.py`
- Modify: `plugins/codex-project-harness/skills/project-runtime/scripts/harness.py`

- [x] Make legacy init create SQLite state and install agent templates.
- [x] Make skill-local CLI proxy to the unified plugin CLI.

### Task 4: Schemas And Validation

**Files:**
- Create: `plugins/codex-project-harness/schemas/acceptance.schema.json`
- Create: `plugins/codex-project-harness/schemas/validation.schema.json`
- Create: `plugins/codex-project-harness/schemas/delivery.schema.json`
- Create: `plugins/codex-project-harness/schemas/adapter.schema.json`
- Create: `plugins/codex-project-harness/schemas/agent.schema.json`
- Modify: existing schema files
- Modify: `plugins/codex-project-harness/scripts/validate_structure.py`

- [x] Add missing entity schemas.
- [x] Align task, project, and quality-gate schemas with runtime entities.
- [x] Require new schemas and runtime scripts in structure validation.

### Task 5: Forward Eval And CI

**Files:**
- Create: `plugins/codex-project-harness/scripts/run_forward_eval.py`
- Create: `docs/runtime/forward-eval-results.json`
- Modify: `.github/workflows/validate.yml`

- [x] Add executable forward eval scenarios.
- [x] Write regression results.
- [x] Run forward eval in CI.

### Task 6: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/runtime/OS_RUNTIME.md`
- Modify: `examples/forward-tests.md`
- Modify: `plugins/codex-project-harness/skills/project-runtime/SKILL.md`

- [x] Document SQLite as the fact source and Markdown as generated views.
- [x] Document unified CLI commands.
- [x] Document scheduler, state machine, event sequence, adapter records, agent registry, quality gate, and delivery behavior.

### Task 7: Verification

**Commands:**

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 -m json.tool plugins/codex-project-harness/.codex-plugin/plugin.json >/dev/null
find plugins/codex-project-harness/schemas -maxdepth 1 -name '*.json' -print -exec python3 -m json.tool {} \; >/dev/null
python3 -m py_compile plugins/codex-project-harness/scripts/*.py plugins/codex-project-harness/skills/project-runtime/scripts/harness.py tests/test_harness_runtime.py tests/test_harness_operating_system.py
python3 -m unittest tests/test_harness_runtime.py tests/test_harness_operating_system.py
python3 plugins/codex-project-harness/scripts/run_forward_eval.py
git diff --check
```

- [x] All commands pass.

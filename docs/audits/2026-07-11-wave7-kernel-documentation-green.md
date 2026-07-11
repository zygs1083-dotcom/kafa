# Wave 7 Kernel Deepening and Documentation Evidence

Date: 2026-07-11

Branch: `v1.26-stop-ship-correctness`

Implementation HEAD: `f300232`

Status: Wave 7 implementation and local verification are green. This document
does not authorize a push, merge, tag, release, or deployment. The release
manifest remains `development` at `1.25.0-beta.1 / runtime 4.18.0 / schema 29`.

## Findings and Commits

| Finding | Commits | Closure evidence |
| --- | --- | --- |
| AR-001 nominal Kernel seams | `54ccc32` | `core.api` is an explicit public contract; `gate_engine` no longer reverse-imports `harness_db`; schema DDL and cycle-scoped read models moved into deep internal modules. |
| AR-002 freeze protected filenames instead of contracts | `7cdc3c7`, `f300232` | Freeze checks protect public CLI, schema/migration compatibility, trust invariants, Plugin/Hook/Skill/runtime-script surfaces, and install/release contracts while permitting internal core modules. Static AST import validation rejects a missing imported local module. |
| DC-001 current documentation drift | `2852f2e`, `83f849b` | Current guidance consistently describes schema 29 and native-host ownership; version chronology remains in CHANGELOG; release validation rejects contradictory schema claims in one release section. |

## AR-001 Deep Module Boundary

Commit `54ccc32` adds or deepens these internal boundaries without changing the
public runtime command surface:

- `core/schema_lifecycle.py` owns schema creation, compatibility columns, and
  lifecycle helpers;
- `core/cycle_ledger.py` owns current-cycle reads, baseline issues,
  traceability, and validation/evidence read models;
- `core/gate_engine.py` consumes explicit dependencies and no longer imports
  the runtime monolith;
- `core/api.py` enumerates the CLI-facing public API instead of dynamically
  re-exporting every non-private name;
- `core/errors.py` provides a stable shared error type for the extracted
  modules.

The extraction reduced `scripts/harness_db.py` from 9,882 lines at the parent
of `54ccc32` to 8,744 lines. The change moved ownership rather than rewriting
the schema: AST comparison of the pre-extraction and post-extraction
`create_schema()` functions found byte-identical 31,910-byte DDL strings and
143 identical `ensure_column()` calls.

Architecture regressions are checked by
`tests/test_kernel_module_architecture.py`:

- every CLI import is present in the explicit `core.api` contract;
- Delivery Decision has no reverse dependency on `harness_db`;
- Schema Lifecycle owns database DDL;
- Cycle Ledger owns the extracted cycle-scoped read models;
- the monolith remains below the 9,000-line extraction threshold.

## AR-002 Contract Freeze and Adversarial Review

The former freeze made every core filename part of the product surface. Commit
`7cdc3c7` narrows the freeze to externally meaningful compatibility:

- public CLI commands and options;
- checked-in schema files and schema version;
- migration and trust contracts;
- Plugin manifest, Hook, Skill, and runtime-script surfaces;
- package, installer, doctor, and release contracts.

Internal core filenames may now evolve. An adversarial review then removed a
false-negative: a referenced internal module could be deleted while an
unreferenced placeholder file kept the structure check green. Commit
`f300232` parses local Python imports with AST and fails closed when an imported
core/script/hook/skill-runtime module is absent. It deliberately permits a new
unreferenced internal module, so the check validates dependency integrity
without recreating a filename freeze.

## DC-001 Current Documentation Contract

Commit `83f849b` rewrites README, INSTALL, QUICKSTART, OS_RUNTIME,
CONTROL_PLANE, and the project-runtime Skill around the current schema 29
behavior:

- native Codex/ChatGPT owns task, thread, subagent, worktree, approval, and
  model lifecycle;
- Kafa owns immutable delivery facts, controller verification, integration,
  and deterministic delivery decisions;
- quickstart stops before independent review and delivery;
- native receipts are audit-only until controller verification;
- discovery and live Hook execution are distinct claims;
- mutable SQLite state remains root-workspace single-writer.

Commit `2852f2e` strengthens release truth by rejecting multiple conflicting
schema statements inside the current release notes section. Current guidance
is checked for behavior, not for a historical version narrative.

## Final Verification on the Assembled Implementation

Wave 7 targeted contract group:

```text
python3 -m unittest -v \
  tests/test_kernel_module_architecture.py \
  tests/test_feature_freeze.py \
  tests/test_install_release.py \
  tests/test_documentation_contract.py \
  tests/test_release_contract.py \
  tests/test_control_plane_architecture.py

Ran 62 tests in 15.004s
OK
```

Complete local Python regression on `f300232`:

```text
Ran 363 tests in 563.030s
OK
```

The complete run emitted four Python 3.14 `ResourceWarning` messages for
unclosed SQLite connections. They did not change the exit code or assertions,
but remain a test-hygiene cleanup risk and are not represented as passing
evidence.

Final deterministic and compatibility matrix:

```text
py_compile: passed
validate_structure.py: passed
runtime smoke: 3 scenarios passed
forward eval: passed (3-scenario runtime smoke wrapper)
skill eval: 15 markers passed
fixture E2E: 5/5 passed
stability E2E: 12/12 passed
  failed_count=0
  false_pass_count=0
  forged_evidence_block_count=1
  sqlite_lock_error_count=0
  human_intervention_count=0
isolated install smoke: passed with codex-cli 0.143.0
real live-codex: 2/2 passed, live_status=passed, skipped=0
release contract: ok=true, release_state=development
repo-scope install plus doctor: ok=true
```

The final live report is `/tmp/kafa-final-live.json`. It records a real Codex
thread/turn and Hook execution plus a native receipt integration path. It also
records `native_subagent_observed=false` and `worktree_owner=live-eval-runner`;
neither capability is overstated as host-native evidence.

## Residual Risks and Exit Decision

Wave 7 closes AR-001, AR-002, and DC-001. Two findings from the original
28-finding audit remain explicitly accepted risks rather than implemented
features:

- ST-001: mutable project facts remain root-workspace, local-only,
  single-writer state; managed worktrees receive immutable packages only;
- AP-001: the public Apps/MCP result surface lacks a host-verifiable receipt
  bound to Kafa action, fence, payload, project, scope, and tool-call identity;
  legacy direct connector results remain audit-only and non-evidentiary.

The implementation objective is locally green, but the repository stays in
development state. No remote CI result, push, merge, tag, release, or
deployment is claimed by this evidence.

## 1. Baseline and execution guardrails

- [x] 1.1 Main model: verify `main` is clean and synchronized, create branch `v2-local-core-slimming`, record HEAD and never overwrite unrelated user changes.
- [x] 1.2 Main model: read `proposal.md`, `design.md`, and `specs/local-delivery-kernel/spec.md`; turn every requirement scenario into a test mapping before editing production code.
- [x] 1.3 Main model: record current metrics in a new audit artifact: Python/test LOC, 54 tables, 129 CLI nodes, 12 Skills, 5 Hooks, plugin size, empty DB size, init time, 5k-fact mutation time, and full-suite time.
- [x] 1.4 Main model: add failing contract tests for the exact 27-table schema 30 inventory and the <=60 CLI, <=7 Skill, and exactly-3 Hook budgets without changing runtime behavior.
- [x] 1.5 Main model: add failing negative tests proving Connector/adapter commands, Host Provider/CSV/native-receipt commands, and same-process HMAC trust must disappear.
- [x] 1.6 Main model: add failing tests for immutable executions, high-risk `human-review-required`, root-controller task transitions, and absence of full-table mutation snapshots.
- [x] 1.7 Checkpoint: run the new tests, confirm they fail only for planned v2 behavior, run the existing full suite to preserve the v1 baseline, and save the red/green command output summary.

## 2. Schema 30 side-by-side migration foundation

- [x] 2.1 Main model: define schema 30 constants and the exact 27-table DDL in `core/schema_lifecycle.py`; keep the production default on schema 29 until migration tests exist.
- [x] 2.2 Main model: implement a SQLite backup helper that records source version, backup path, SHA-256 digest, row counts, and integrity results without serializing secrets into events or Markdown.
- [x] 2.3 Main model: implement schema 29 -> schema 30 staging conversion in a new internal migration module, copying only the approved local facts and leaving the active DB untouched.
- [x] 2.4 Main model: convert old controller-generated command evidence into immutable executions and map eligible validation links; mark unbound/manual validations invalidated rather than gate-eligible.
- [x] 2.5 Main model: implement the published schema 27 and development schema 28 -> schema 29 staging path using isolated legacy migration code, followed by schema 30 conversion.
- [x] 2.6 Main model: add failure injection before copy, during relation copy, during invariant validation, before atomic replace, and after replace; prove source preservation or automatic backup restore.
- [x] 2.7 Main model: add migration tests for cycle-local IDs, current candidate, gate supersession, accepted-risk expiry, invalidations, findings, and delivery history.
- [x] 2.8 Spark-eligible after DDL is locked: update deterministic schema inventory constants and JSON schema file lists; main model reviews every removed/added item.
- [x] 2.9 Checkpoint: run schema lifecycle, migration, freeze, foreign-key, doctor, and rollback tests; inspect both a schema 27 and schema 29 migrated DB with `sqlite3`.

## 3. Local trust and delivery decision

- [x] 3.1 Main model: replace Connector HMAC/CI/external-session trust branches in the delivery engine with `controller-verified`, `reviewed-local`, `same-context-degraded`, and `human-review-required` semantics.
- [x] 3.2 Main model: make active high/critical failure modes require structured current-candidate execution and distinct producer/reviewer context metadata, then block autonomous delivery with `human-review-required`.
- [x] 3.3 Main model: preserve the explicit accepted/exempt risk path only when actor, reason, scope, revision, and unexpired expiry are complete; label it procedural rather than cryptographic.
- [x] 3.4 Main model: ensure current-cycle/current-candidate selection, latest gate ordering, open structured findings, dirty Git state, stale artifacts, and sandbox/no-network policy remain fail-closed.
- [x] 3.5 Main model: add adversarial tests for forged session IDs, same-context review, manual HMAC-looking tokens, stale CI-looking facts, accepted-risk expiry, and direct DB tampering.
- [x] 3.6 Checkpoint: run delivery-cycle, structured-result, sandbox-policy, finding, stop-ship, and trust tests; no high-risk path may pass without the new explicit conditions.

## 4. Remove external Connector runtime

- [x] 4.1 Main model: remove Connector profile, namespace, scope, retry, throttle, HTTP/`gh`, marker recovery, outbox, advisory fallback, and adapter functions from runtime implementation and public API.
- [x] 4.2 Main model: remove `connector` and `adapter` parsers/dispatch branches and return a concise major-version migration message for retired invocations at the outer compatibility boundary only.
- [x] 4.3 Main model: remove Connector/adapter/external verification schema definitions, project columns, projection columns, doctor checks, invariant checks, snapshot lists, and active schema tables.
- [x] 4.4 Spark-eligible after 4.1-4.3: delete dedicated Connector test files and remove Connector-only assertions from shared tests; main model verifies no delivery negative coverage was lost.
- [x] 4.5 Spark-eligible after 4.1-4.3: remove external-tool references from Agent templates, Skill examples, installer required inventories, and generated view headings using an exact `rg` checklist.
- [x] 4.6 Main model: add a static runtime test rejecting `gh api`, provider endpoint constants, Connector token environment variables, and direct GitHub/Linear/Notion/Figma/Slack clients under Plugin Kernel code.
- [x] 4.7 Checkpoint: run all non-Host tests, initialize a greenfield project without external credentials, inspect network-call inventory, and confirm no external projections or retired tables exist.

## 5. Remove duplicate Host and dispatch lifecycle

- [x] 5.1 Main model: delete `HostCodexProvider`, worker/watchdog entrypoints, process-tree cancellation, fake provider implementation, model/Spark policy, and `openai-codex` optional dependency.
- [x] 5.2 Main model: remove provider start/status/collect/cancel/reconcile, CSV export/import, native receipt export/import, Kafa worktree creation, file claims, integration, and provider report public APIs.
- [x] 5.3 Main model: remove provider, report, fanout, dispatch, worktree, file-claim, integration-attempt, agent-capability, and task-attempt facts from active schema 30 and migration output.
- [x] 5.4 Spark-eligible after APIs are removed: delete Host Provider, provider lifecycle, fanout, native receipt, integration, file-claim, and dispatch-status test files; main model reviews remaining local verification coverage.
- [x] 5.5 Main model: replace provider-oriented E2E setup with one Native Codex ownership contract test that verifies Kafa does not spawn, own, or impersonate host lifecycle.
- [x] 5.6 Main model: update `pyproject.toml`, install tests, structure validation, release contract, and isolated install smoke so the base and full installation contain no Host SDK dependency.
- [x] 5.7 Checkpoint: run package/install tests, inspect process-spawn and dependency inventories, and prove ordinary Plugin use and local verification work with no `openai_codex` import available.

## 6. Simplify root-controller task lifecycle

- [x] 6.1 Main model: migrate task statuses and columns to the planned/active/submitted/accepted/blocked/cancelled single-writer model while preserving cycle identity and audit revision.
- [x] 6.2 Main model: implement `task add/list/start/submit/accept/block/cancel` preconditions and remove claim, heartbeat, lease, fence, review lease, retry, release, and stale-recovery mechanics.
- [x] 6.3 Main model: remove lock-manager/scheduler code that exists only for worker DB writers; retain SQLite busy handling needed for controller/admin contention.
- [x] 6.4 Main model: update Skills so subagents return code/review results to the root controller and never invoke mutating Kafa runtime commands themselves.
- [x] 6.5 Main model: add state-transition, retry/idempotence, producer/reviewer context separation, illegal direct SQL mutation, and concurrent admin-read tests.
- [x] 6.6 Checkpoint: run task, cycle, cold-start, hooks, idempotence-replacement, and SQLite tests; manually execute one task lifecycle without lease/fence values.

## 7. Normalize execution and validation

- [x] 7.1 Main model: add the immutable `executions` table and `validation_executions` relation with current-cycle/current-candidate foreign-key and uniqueness constraints.
- [x] 7.2 Main model: move local/container execution and structured result parsing behind `core/execution.py`, preserving command-template policy, artifact digest, positive count, sandbox, and no-network checks.
- [x] 7.3 Main model: add `verify run` so command execution occurs outside the write transaction and successful fact insertion is atomic after result validation.
- [x] 7.4 Main model: reduce `validations` to judgment/supersession fields and make delivery decisions read linked immutable executions instead of copied command columns.
- [x] 7.5 Main model: remove manual `evidence record`, `test record`, old evidence/tests/sandbox-execution tables, duplicated command fields, and related schemas/projections.
- [x] 7.6 Main model: update quickstart so it creates a target, runs `verify run`, records a submitted task, and stops before independent review without synthesizing trusted QA.
- [x] 7.7 Main model: add tests for immutable overwrite rejection, malformed/zero/failed structured results, stale candidate, artifact digest mismatch, local/container execution, and manual evidence forgery.
- [x] 7.8 Checkpoint: run executor, structured-result, sandbox, quickstart, delivery, migration-conversion, and negative evidence tests; inspect one execution-to-validation trace manually.

## 8. Remove whole-database event sourcing and target projections

- [x] 8.1 Main model: remove transaction-wide before/after snapshots, canonical mutation diffs, runtime snapshots, event replay, JSON checkpoint import/export, and public checkpoint/event commands.
- [x] 8.2 Main model: retain compact append-only audit events with entity, actor, command, before/after summary, correlation id, and timestamp; document that events are not a full recovery source.
- [x] 8.3 Main model: use SQLite backup for migration/admin recovery and add backup integrity tests rather than replay reconstruction tests.
- [x] 8.4 Main model: make each mutation identify affected projections and rebuild only those views; retain explicit full `projection rebuild` for repair.
- [x] 8.5 Main model: remove tooling-map/advisory-fallback projections and all external-only columns from remaining local views.
- [x] 8.6 Spark-eligible after projection contracts are locked: update deterministic Markdown headers and documentation snapshots; main model verifies semantics and paths.
- [x] 8.7 Main model: add a benchmark harness comparing baseline and schema 30 init, DB size, 5k-fact mutation, targeted projection, and test time without hard-coding flaky CI timing assertions.
- [x] 8.8 Checkpoint: run audit-event, backup, projection, doctor, invariant, and benchmark tests; verify normal transactions do not enumerate unrelated tables.

## 9. Reduce Skills, templates, Hooks, and installer contract

- [x] 9.1 Main model: merge bootstrap/runtime/delivery guidance into `project-harness` and route planning/spec work to OpenSpec while preserving the local Kernel as delivery authority.
- [x] 9.2 Main model: remove standalone `project-bootstrap`, `project-runtime`, `requirement-baseline`, `team-architecture`, and `delivery-readiness` Skills after their required local behavior is covered elsewhere.
- [x] 9.3 Main model: retain only project-harness, minimal-safe-change, bug-fix-loop, test-first-delivery, independent-quality-gate, harness-audit, and project-retrospective Skill entrypoints.
- [x] 9.4 Spark-eligible after Skill contracts are locked: reduce Agent templates to developer, architect, and qa-reviewer and remove external/legacy wording from those three files.
- [x] 9.5 Main model: reduce Hooks to SessionStart, SubagentStart, and Stop; remove default Pre/Post tool hooks and preserve friendly uninitialized-project behavior.
- [x] 9.6 Main model: update `kafa` installer/doctor inventories and control-plane checks to validate the local architecture rather than Connector/provider markers.
- [x] 9.7 Main model: add isolated install and app-server discovery tests for the exact seven Skills, three Hooks, three templates, and local runtime files.
- [x] 9.8 Checkpoint: install into an isolated HOME, verify `codex plugin list`, app-server discovery, hook execution, project init/status, and absence of removed files.

## 10. Rebuild evals, documentation, and release truth

- [x] 10.1 Main model: replace fixture/stability scenarios with the twelve local-only scenarios defined in the spec and keep false-pass, lock-error, skipped-live, and human-intervention thresholds truthful.
- [x] 10.2 Main model: rewrite the opt-in live Codex profile to let the real host edit a local candidate and then run controller verification without provider/receipt lifecycle.
- [x] 10.3 Spark-eligible after behavior is green: remove superseded Connector/Host ADR content and rewrite README, INSTALL, QUICKSTART, OS_RUNTIME, CONTROL_PLANE, examples, and Skill references to one local user journey.
- [x] 10.4 Main model: centralize version/runtime/schema values so SDK metadata, docs, structure validation, tests, release manifest, and changelog cannot retain stale literals.
- [x] 10.5 Main model: set `VERSION=2.0.0-beta.1`, package `2.0.0b1`, Runtime/Kernel `5.0.0`, schema 30, and release state `development`; add a breaking migration changelog entry.
- [x] 10.6 Main model: update three-platform CI and release workflow to remove retired tests/dependencies while retaining isolated installation, local stability, and real Native Codex compatibility gates.
- [x] 10.7 Main model: create a final before/after audit with files, LOC, tables, indexes, schemas, commands, Skills, Hooks, DB size, plugin size, mutation time, and test duration.
- [x] 10.8 Checkpoint: run documentation contract, release contract, structure validation, eval tests, isolated install, and OpenSpec validation; search for every retired external/provider term and classify any intentional historical occurrence.

## 11. Final regression and handoff

- [x] 11.1 Run `python3 -m py_compile` over root package, Plugin core/scripts/hooks, remaining Skill proxy files, and all tests.
- [x] 11.2 Run `python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness` and `openspec validate local-core-slimming`.
- [x] 11.3 Run the complete `python3 -m unittest discover -s tests -p 'test_*.py'` suite on the primary platform with ResourceWarning promoted to error.
- [x] 11.4 Run runtime smoke, skill eval, local fixture/stability E2E, migration/rollback matrix, `kafa doctor --repo . --json`, and `git diff --check`.
- [x] 11.5 Run isolated wheel/source install smoke and verify plugin registration, cache digest, exact Skill/Hook/template discovery, project init, quickstart, and uninstall.
- [x] 11.6 Run or explicitly report the opt-in real Native Codex profile; blocked/not-run MUST NOT be described as pass.
- [x] 11.7 Main model: perform adversarial review for false delivery, data loss, stale candidate, manual evidence forgery, high-risk bypass, migration rollback, dirty tree, removed-network calls, and simpler alternatives.
- [x] 11.8 Main model: inspect `git status`, diff scope, generated files, and user changes; do not commit, push, merge, tag, release, or deploy until the user explicitly approves that next action.
- [x] 11.9 Final handoff: report completed tasks, exact verification evidence, before/after metrics, migration and rollback behavior, residual risks, and any spec deviation requiring user acceptance.
- [x] 11.10 Audit remediation: add regression tests for side-effect-free maintenance-script `--help`, truthful agent/eval metrics, Native Host model-ownership inventory, root-only template guidance, and reviewer read-only boundaries; capture red failures for each confirmed behavior defect.
- [x] 11.11 Spark-eligible mechanical task: align the three Agent templates with the locked single-writer and producer/reviewer contracts; main model reviews the exact diff and deterministic tests.
- [x] 11.12 Main model: make runtime smoke/eval command contracts explicit, remove fabricated agent completion/retry metrics, and record observable Native Host runtime/token telemetry without adding Kafa-owned model routing or trust claims.
- [x] 11.13 Main model: add a compact Host delegation matrix with dependency, exclusive/shared files, acceptance, targeted/integration tests, capability hint, context/output budget, and deterministic escalation triggers; actual model selection remains Native Host-owned.
- [x] 11.14 Main model: add an opt-in real Native Host two-producer integration scenario covering disjoint edits, overlap rejection or explicit serialization, deterministic integration order, combined regression, and controller verification; fixture/not-run results MUST NOT be called pass.
- [x] 11.15 Checkpoint: run the new red/green targeted tests, structure validation, fixture/stability eval, opt-in live profiles when available, OpenSpec validation, and `git diff --check`; preserve exact not-run or human-review-required status.
- [x] 11.16 Final re-audit: rerun the full ResourceWarning-as-error suite, update persistent live/token evidence and before/after metrics, inspect audit side effects and effective-installation drift, and repeat adversarial review before handoff.

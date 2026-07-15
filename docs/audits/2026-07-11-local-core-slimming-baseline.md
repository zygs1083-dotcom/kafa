# Local Core Slimming Baseline And Scenario Map

## Audit identity

- Change: `local-core-slimming`
- Branch: `v2-local-core-slimming`
- Baseline: `main@adba3691d859c0ffc93d75cc148d8f916314cc49`
- Baseline remote: `origin/main@adba3691d859c0ffc93d75cc148d8f916314cc49`
- Release state at baseline: `development`
- Scope authority: `openspec/changes/archive/2026-07-15-local-core-slimming/`
- Implementation ledger: `openspec/changes/archive/2026-07-15-local-core-slimming/tasks.md`
- Workspace note: the untracked `openspec/` tree is the user-supplied change specification and was preserved when the branch was created.

## Scenario-to-test map

Every scenario in `specs/local-delivery-kernel/spec.md` has an executable test target before production removal begins. Test names below are the contract; files may gain helpers, but a scenario may not be silently dropped or weakened.

| Requirement | Scenario | Planned executable test |
| --- | --- | --- |
| Local-only runtime boundary | Greenfield local project | `tests/test_local_core_contract.py::LocalOnlyRuntimeContractTests.test_greenfield_requires_no_external_credentials` |
| Local-only runtime boundary | Removed external command | `tests/test_local_core_contract.py::LocalOnlyRuntimeContractTests.test_retired_connector_and_adapter_commands_fail_before_mutation` |
| Local-only runtime boundary | Generated local views | `tests/test_local_core_contract.py::LocalOnlyRuntimeContractTests.test_greenfield_omits_external_projections` |
| Native host lifecycle ownership | Native subagent edits code | `tests/test_native_host_ownership.py::NativeHostOwnershipTests.test_controller_verifies_current_candidate_without_provider_session` |
| Native host lifecycle ownership | Legacy provider request | `tests/test_native_host_ownership.py::NativeHostOwnershipTests.test_retired_provider_paths_cannot_spawn_or_mutate` |
| Root-controller single writer | Task progresses normally | `tests/test_single_writer_tasks.py::SingleWriterTaskTests.test_planned_active_submitted_accepted` |
| Root-controller single writer | Illegal task transition | `tests/test_single_writer_tasks.py::SingleWriterTaskTests.test_accept_rejects_planned_and_active_without_mutation` |
| Root-controller single writer | Worker tries to mutate state | `tests/test_single_writer_tasks.py::SingleWriterTaskTests.test_subagent_skill_returns_results_to_root_controller` |
| Immutable controller executions | Passing local execution | `tests/test_execution_validation.py::ImmutableExecutionTests.test_verify_run_atomically_records_execution_validation_links_and_event` |
| Immutable controller executions | Duplicate execution id | `tests/test_execution_validation.py::ImmutableExecutionTests.test_execution_insert_is_immutable` |
| Immutable controller executions | Manual command claim | `tests/test_execution_validation.py::ImmutableExecutionTests.test_manual_claim_cannot_create_gate_eligible_execution` |
| Immutable controller executions | Structured result is missing | `tests/test_execution_validation.py::ImmutableExecutionTests.test_structured_result_missing_malformed_failed_or_zero_fails_closed` |
| Candidate-scoped local delivery decision | Candidate changed after verification | `tests/test_local_delivery_policy.py::CandidateScopedDeliveryTests.test_stale_execution_and_gate_do_not_satisfy_current_candidate` |
| Candidate-scoped local delivery decision | Open high finding | `tests/test_local_delivery_policy.py::CandidateScopedDeliveryTests.test_open_high_or_critical_finding_blocks_passing_gate` |
| Candidate-scoped local delivery decision | New cycle | `tests/test_local_delivery_policy.py::CandidateScopedDeliveryTests.test_cycle_isolation_prevents_pass_and_failure_leakage` |
| Honest local high-risk policy | High-risk autonomous attempt | `tests/test_local_delivery_policy.py::HonestHighRiskPolicyTests.test_high_risk_without_acceptance_returns_human_review_required` |
| Honest local high-risk policy | Same-context review | `tests/test_local_delivery_policy.py::HonestHighRiskPolicyTests.test_same_context_high_risk_gate_is_rejected` |
| Honest local high-risk policy | Explicit risk acceptance | `tests/test_local_delivery_policy.py::HonestHighRiskPolicyTests.test_complete_unexpired_acceptance_uses_procedural_path` |
| Minimal schema 30 | Fresh schema inventory | `tests/test_schema30_contract.py::Schema30ContractTests.test_greenfield_has_exact_approved_27_tables` |
| Minimal schema 30 | Schema invariant check | `tests/test_schema30_contract.py::Schema30ContractTests.test_unknown_or_retired_table_fails_structure_contract` |
| Recoverable migration to schema 30 | Schema 29 migration succeeds | `tests/test_schema30_migration.py::Schema30MigrationTests.test_schema29_backup_convert_validate_and_activate` |
| Recoverable migration to schema 30 | Published schema 27 upgrade | `tests/test_schema30_migration.py::Schema30MigrationTests.test_schema27_uses_isolated_legacy_staging_then_schema30` |
| Recoverable migration to schema 30 | Migration failure before activation | `tests/test_schema30_migration.py::Schema30MigrationTests.test_pre_activation_failures_preserve_source_bytes` |
| Recoverable migration to schema 30 | Post-activation doctor failure | `tests/test_schema30_migration.py::Schema30MigrationTests.test_post_activation_doctor_failure_restores_verified_backup` |
| Bounded local transaction cost | Large local ledger mutation | `tests/test_local_transaction_cost.py::LocalTransactionCostTests.test_requirement_mutation_does_not_enumerate_unrelated_tables` |
| Bounded local transaction cost | Audit event | `tests/test_local_transaction_cost.py::LocalTransactionCostTests.test_mutation_appends_compact_non_replay_event` |
| Targeted projections | Requirement update | `tests/test_targeted_projections.py::TargetedProjectionTests.test_requirement_mutation_rebuilds_only_affected_views` |
| Targeted projections | Projection recovery | `tests/test_targeted_projections.py::TargetedProjectionTests.test_admin_rebuild_restores_all_local_views` |
| Reduced plugin surface | Isolated user installation | `tests/test_install_release.py::InstallReleaseTests.test_isolated_install_discovers_exact_local_surface` |
| Reduced plugin surface | Ordinary project without initialization | `tests/test_codex_hooks.py::CodexHookTests.test_uninitialized_hooks_skip_without_writes_or_traceback` |
| Truthful local evaluation matrix | Stability profile | `tests/test_agent_e2e.py::AgentE2ETests.test_stability_profile_covers_local_only_matrix` |
| Truthful local evaluation matrix | Live Codex profile disabled | `tests/test_agent_e2e.py::AgentE2ETests.test_live_profile_unavailable_is_not_run_or_blocked_not_pass` |
| Truthful local evaluation matrix | Live Codex profile succeeds | `tests/test_agent_e2e.py::AgentE2ETests.test_live_host_edits_then_controller_verifies_without_provider` |

## Wave ownership

The main model owns schema, migration, trust, delivery gates, execution normalization, public API removal, and integration. A subagent may only take a task explicitly marked Spark-eligible in `tasks.md` after the main-model contract for that slice is green or structurally locked; the root controller rechecks its diff and deterministic acceptance command.

## Baseline metrics

These measurements were taken on the primary macOS workspace before any production behavior changed. Timing budgets are comparative reference-machine evidence, not CI assertions.

| Metric | Baseline result | Measurement |
| --- | ---: | --- |
| Tracked Python LOC | 33,521 | `git ls-files '*.py' \| xargs wc -l` |
| Tracked test LOC | 13,251 | `git ls-files 'tests/*.py' \| xargs wc -l` |
| Plugin Python LOC | 18,878 | `git ls-files 'plugins/codex-project-harness/**/*.py' \| xargs wc -l` |
| Active runtime tables | 54 | Fresh init plus `sqlite_master`, excluding `sqlite_%` tables |
| Runtime indexes | 67 | Fresh init plus all `sqlite_master` rows with `type='index'` |
| JSON schemas | 40 | Files directly under `plugins/codex-project-harness/schemas/` |
| CLI parser nodes | 129 | Recursive walk of `argparse._SubParsersAction` choices from `harness.build_parser()` |
| Skill entrypoints | 12 | Directories directly under `plugins/codex-project-harness/skills/` |
| Default hooks | 5 | Hook entries in `hooks/hooks.json` |
| Plugin directory | 1,276 KiB | `du -sk plugins/codex-project-harness` |
| Fresh empty database | 552,960 bytes | `stat` on `.ai-team/state/harness.db` immediately after init |
| Fresh init wall time | 0.31 s | `/usr/bin/time -p ... harness.py --root <temp> init` |
| 5,000-fact single mutation | 0.146113 s median | Five in-process `add_requirement` calls after seeding 5,000 decision rows; samples 0.146809, 0.146358, 0.145786, 0.146113, 0.145475 s |
| Full unittest | 370 tests in 406.457 s; 406.72 s wall | `PYTHONDONTWRITEBYTECODE=1 /usr/bin/time -p python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'` |

The baseline full suite exited zero with `OK`. A skipped, blocked, fixture-only, or not-run profile is not represented as a pass in this table.

## Wave 0 checkpoint

### Target v2 contracts: expected red

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests/test_schema30_contract.py \
  tests/test_local_core_contract.py \
  tests/test_native_host_ownership.py \
  tests/test_single_writer_tasks.py \
  tests/test_execution_validation.py \
  tests/test_local_delivery_policy.py \
  tests/test_local_transaction_cost.py

Ran 28 tests in 4.069s
FAILED (failures=27)
```

The one green test proves the pre-existing local init/status path already works without external credentials. The 27 expected failures map only to locked v2 differences:

- schema 29/54 tables instead of schema 30/27 tables;
- 129 CLI nodes, 12 Skills, and 5 Hooks instead of the reduced budgets;
- active Connector/adapter/HMAC and Host Provider/CSV/native-receipt surfaces;
- lease/fence task coordination instead of the root-controller lifecycle;
- no immutable `executions` or `verify run` path and still-active manual evidence/test commands;
- no `core.delivery` honest local high-risk policy;
- runtime snapshots, canonical mutation payloads, and whole-table scans still active.

No failure was caused by a missing fixture, network request, provider spawn, syntax error, or unrelated existing regression.

### Existing v1 suite: green control

```text
PYTHONDONTWRITEBYTECODE=1 /usr/bin/time -p sh -c \
  'git ls-files "tests/test_*.py" | xargs python3 -W error::ResourceWarning -m unittest'

Ran 370 tests in 389.631s
OK
real 389.85
```

This command intentionally selects only tests tracked at `main@adba369`, so the expected-red v2 contracts do not get misreported as v1 regressions. `git diff --check` and `openspec validate local-core-slimming` both exited zero at the checkpoint.

## Wave 1 checkpoint

The side-by-side schema 30 foundation remained isolated from the production default while migration, rollback, and compatibility behavior were exercised.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest \
  tests.test_schema30_migration \
  tests.test_schema30_contract.Schema30ContractTests.test_json_schema_inventory_and_execution_contract_are_locked \
  tests.test_schema30_contract.Schema30ContractTests.test_staging_factory_creates_only_the_locked_schema30_inventory

Ran 11 tests in 0.995s
OK

PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest \
  tests.test_schema_lifecycle tests.test_schema29_migration

Ran 26 tests in 5.456s
OK
```

The first feature-freeze run correctly failed because the newly added `execution.schema.json` had not yet been added to both deterministic installer inventories. That run is not counted as passing. After updating `scripts/validate_structure.py` and `kafa/cli.py`, the combined schema-inventory and feature-freeze rerun completed 11 tests in 1.407 seconds with `OK`; the production default was still schema 29 at this boundary.

Migration tests exercised all five failure-injection points: before copy, during relation copy, during invariant validation, before atomic replace, and after atomic replace. Pre-activation failures preserved source bytes; the post-activation failure restored the verified backup. Successful activation ran the internal schema 30 doctor, including inventory, integrity, foreign-key, immutable-trigger, and activated-migration checks.

Manual `/usr/bin/sqlite3 -readonly` inspection of independently generated fixtures recorded:

| Source | Result | Tables | Retired tables | Immutable triggers | Integrity | Foreign-key issues | Preserved evidence |
| --- | --- | ---: | ---: | ---: | --- | ---: | --- |
| schema 27 | schema 30 / Runtime 5.0.0 | 27 | 0 | 4 | `ok` | 0 | 27 -> 29 `legacy-history`; 29 -> 30 `activated` |
| schema 29 | schema 30 / Runtime 5.0.0 | 27 | 0 | 4 | `ok` | 0 | sentinel decision plus 29 -> 30 `activated` |

No migrated database retained Connector, CI verification, external-session, or Host provider tables.

## Wave 1 trust checkpoint

The delivery engine no longer imports Connector trust code or queries CI/external-session verification tables. Local trust is classified as `controller-verified`, `reviewed-local`, `same-context-degraded`, or `human-review-required`; an explicit all-risk acceptance uses `accepted-risk` with `trust_level=procedural`.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest \
  tests.test_delivery_cycles \
  tests.test_structured_test_results \
  tests.test_target_sandbox_policy \
  tests.test_sandbox_execution \
  tests.test_stop_ship_regressions \
  tests.test_local_delivery_policy

Ran 42 tests in 78.286s
OK
```

Four legacy schema 29 tests that create HMAC/CI/external-session-looking facts were inverted to assert the new negative contract; they completed in 24.793 seconds with `OK`. Two additional expiry/open-critical checks completed in 8.330 seconds with `OK`.

The adversarial matrix covers forged producer/reviewer ids, same-context review, HMAC-looking text, stale CI-looking text, missing and duplicate same-level risk acceptance, expired acceptance, stale accepted revision, direct quality-gate tampering, immutable execution overwrite, dirty Git, stale candidate, latest failing gate, open high/critical findings, invalidations, and sandbox/no-network mismatches. High/critical work with otherwise valid structured execution and distinct context metadata still returns `human-review-required`; only complete, current-revision, unexpired accepted/exempt facts for every risk can enter the explicitly procedural path.

## Wave 2 external-runtime removal checkpoint

Connector/adapter commands, schemas, active tables, projections, HMAC/session-attestation entrypoints, provider endpoints, credential environment names, and direct SaaS client code were removed from the business runtime. Retired CLI invocations stop at the outer compatibility boundary before mutating the database. Legacy Connector identifiers remain only in the schema 27/28/29 conversion filters and the two-command removal message; the static runtime contract classifies those paths and rejects endpoint, credential, `gh api`, or provider-client reintroduction.

Relevant green verification after fixing two removal-induced regressions (an evidence insert arity mismatch and missing legacy-table/column guards) was:

```text
Local runtime boundary plus delivery policy: 19 tests, OK
Delivery cycles plus sandbox migration: 10 tests, OK
Stop-ship regressions: 8 tests, OK
Shared operating-system suite: 60 tests exercised; one stale wording assertion was updated, then its exact rerun passed
Schema 29/30 migration, schema inventory, and feature freeze: 30 tests, OK
Install/release/local-only/doc contracts excluding the known later-wave documentation case: 51 tests, OK
Runtime smoke: 2 local scenarios, OK
Plugin structure validation: OK
OpenSpec validation: valid
git diff --check: clean
```

A credential-free greenfield schema 29 transition database completed `init`, `status`, and Plugin `doctor`, had 46 active tables, zero Connector/adapter/external-verification tables, no external projections, and `pragma integrity_check=ok`. This is not described as the final schema 30 greenfield result: production activation to the exact 27-table schema remains a later task.

The non-Host sweep was run, but was not all green and is therefore not represented as passing. Its remaining failures map to already-red later-wave contracts: 11/11 execution/transaction/single-writer tests, three CLI/Skill/Hook surface-budget tests, two schema30-production-default tests, and one documentation assertion for the retired reviewer-attestation syntax. Host/provider/dispatch/receipt-specific files were intentionally outside this Wave 2 non-Host selection and remain required in Wave 5. No skipped, blocked, not-run, fixture-only, or planned-red result is counted in the green totals above.

## Wave 2 legacy Host removal checkpoint

The Host SDK worker/watchdog implementation, fixture provider implementation, Kafa-owned worktree and file-claim machinery, CSV/native-receipt exchange, provider/report/fanout public APIs, all dispatch/agent/session CLI parsers, twelve Host/dispatch JSON schemas, and the `openai-codex` optional dependency were removed. Retired invocations fail at the outer major-version boundary before database mutation. Native Codex/ChatGPT remains the sole task, subagent, worktree, approval, model, cancel, and handoff owner.

```text
Native Host negative contract plus schema30 Host-table filtering: 6 tests, OK
Feature-freeze inventory after Host surface deletion: 9 tests, OK
Install/package/isolated-wheel suite: 26 tests, OK
Plugin structure validation: OK
Local executor with openai_codex import explicitly blocked: OK, exit 0, executed_count 1
Plugin init/status with openai_codex import explicitly blocked: OK
Package metadata: dependencies=[], no optional-dependencies, no Host SDK extra
```

The process-spawn inventory contains only the local controller executor, local Git/content-identity probes, and compatibility wrapper subprocesses. It contains no background `Popen`, worker/watchdog entrypoint, process-tree kill, `openai_codex` import, Host model policy, or Spark policy. The first in-process CLI probe used an unsupported positional call to `main()` and raised `TypeError`; that diagnostic attempt is not counted as pass. After reading the actual zero-argument entrypoint, the verified `sys.argv` invocation completed `init` and `status` with the Host SDK import guard active.

The positive `verify run` half of the Native Host scenario remains a planned Wave 4 execution-normalization red test and is not counted above. Likewise the final twelve-scenario eval matrix and live Native Codex profile remain Task 10 work; no fixture, disabled, blocked, or not-run profile is described as compatible.

## Wave 3 single-writer task checkpoint

The public task surface is now exactly `add/list/start/submit/accept/block/cancel`. Fresh task rows use only `planned/active/submitted/accepted/blocked/cancelled`; lease tokens, heartbeat/expiry, retry budget/count, fences, claim/release/recover-stale, reviewer leases, and direct status update commands are absent. Native workers return code or review results through the host, and only the root controller mutates task facts.

The primary green checkpoint was:

```text
Single-writer state, transition, idempotency, context, SQL, and concurrent-read tests: 10 tests, OK
Command idempotency plus Store transaction/rollback tests: 10 tests, OK
Codex Hook boundary tests: 13 tests, OK
Schema 27/28/29 -> 30 task migration and cycle-identity tests: 14 tests, OK
Combined command: 47 tests, OK
Operating-system lifecycle-focused assertions: 5 tests, OK
git diff --check: clean
```

The migration matrix now explicitly maps legacy `failed -> blocked` and `skipped -> cancelled`, preserving the task rows without presenting either legacy status as accepted work. Schema 27/28 isolated staging and schema 29 side-by-side conversion remained green after the transitional schema 29 task DDL was reduced.

A manual CLI walk produced:

```text
planned revision 1 -> active -> submitted(context=manual-producer) -> accepted revision 4
accepted_by=root-controller
retired task columns=[]
```

The broader checkpoint selections were also run but were not fully green and are not counted as passing:

- delivery-cycle plus legacy runtime validation: 13 tests, 4 passed and 9 errored only at the removed `dispatch run` evidence helper;
- cold-start guided loop: 8 tests, 5 passed and 3 failed only at the still-pending `dispatch_plan` quickstart execution path;
- stop-ship regression file: 8 tests, 2 passed and 6 failed only at the same pending Wave 4 execution/quickstart replacement;
- full operating-system file retained Wave 4 failures at its removed dispatch evidence helper, while its five task-lifecycle assertions passed independently.

Those are planned `verify run`/immutable-execution work in the next Wave. They are not skipped and are not described as passing. No lifecycle failure remained before those execution boundaries.

## Wave 4 immutable execution and validation checkpoint

Greenfield runtime activation now creates the exact 27-table schema 30 and Runtime `5.0.0`. Controller command facts are insert-only `executions`; validation rows contain judgment and supersession fields only and link through `validation_executions`. `verify run` reads a registered target, executes outside the SQLite write transaction, then rechecks cycle, candidate, target, acceptance/failure-mode links, artifact digest, semantic result, count, sandbox, no-network, and policy inside the atomic fact transaction.

The targeted non-legacy checkpoint was:

```text
python3 -B -m unittest \
  tests.test_execution_validation \
  tests.test_structured_test_results \
  tests.test_cold_start_guided_loop \
  tests.test_local_delivery_policy \
  tests.test_schema30_migration \
  tests.test_schema30_contract \
  tests.test_native_host_ownership

Ran 55 tests in 8.170s
OK
```

This set covers a real CLI quickstart -> immutable verify -> submitted task -> independent review return -> accepted task -> reviewed-local gate -> delivered cycle, not only direct SQL fixtures. It also covers update/delete immutability, missing/malformed/failing/zero structured results, candidate drift during execution, artifact tampering, atomic rollback when event insertion fails, local execution, container `--network none` construction, container-unavailable fail-closed behavior, manual validation forgery, Native Host ownership, schema 29 conversion, and all five migration failure-injection/rollback points. The direct schema 30 policy tests remain valid policy evidence but are not described as delivery E2E on their own.

Quickstart now creates requirement, acceptance, task, target, baseline, and one immutable execution; it leaves the task `submitted`, quality gates `0`, deliveries `0`, and prints an explicit independent-review stop. No synthetic QA or same-context gate is created. The shared structured-result and cold-start suites were migrated only after the `verify run` interface was locked; their isolated rerun completed 12 tests with `OK`.

The delivery E2E exposed and closed a revision deadlock: the `qa -> delivery_readiness` phase transition required a passing current-revision gate but used to increment the revision and immediately stale that same gate. Phase is now treated as a procedural cursor and retains the candidate/fact revision while still appending an audit event. Task, scope, risk, and other delivery-fact mutations continue to advance revision, so stale-gate protection is not weakened.

Manual execution-to-validation inspection reported:

```text
candidate_match=True
target_exit_count_semantic=('TRACE-UNIT', 0, 1, 'pass')
runner_sandbox_network_policy=('local', '', 0, 'allowed')
artifact_digest_match=True
validation_result_status=('pass', 'active')
```

`python3 -m py_compile` over changed execution/runtime/tests, Plugin structure validation, `openspec validate local-core-slimming`, and `git diff --check` all exited zero at this checkpoint.

Legacy `tests/test_delivery_cycles.py`, `tests/test_harness_runtime.py`, `tests/test_sandbox_execution.py`, `tests/test_target_sandbox_policy.py`, and dispatch-dependent portions of `tests/test_harness_operating_system.py` are not counted in the 55-test green result. Their remaining removed-dispatch/manual-evidence setup must be migrated or deleted according to later tasks before the full regression; no failure, skipped container, or fixture-only result is represented as passing.

## Wave 5 bounded transaction and targeted projection checkpoint

Normal schema 30 transactions no longer enumerate runtime tables before and after a mutation. JSON checkpoints, event export/validation, event replay/rebuild, canonical mutation journals, and the runtime snapshot schema are absent from the public runtime. The remaining schema 29 `runtime_snapshots` DDL is confined to the isolated 27/28 -> 29 migration stage and never enters the 27-table schema 30 active database.

Events are append-only compact audit rows with entity, actor, command, bounded before/after summaries, correlation id, and timestamp. Arbitrary payload fields such as Connector tokens are excluded by the summary whitelist. Schema 29 local audit events receive non-empty entity and correlation identities during conversion. Events are not a recovery source; migration and administrator repair use verified SQLite backups with digest, row counts, integrity check, and foreign-key results.

Ordinary mutations now call an explicit ordered projection registry. A requirement mutation rebuilds exactly `project-state`, `requirements`, and `traceability`; damaged delivery and finding views remain untouched. `projection rebuild` remains the full admin recovery path and restored a damaged generated view in the checkpoint.

The Wave 5 checkpoint was:

```text
Audit-event, backup/rollback, projection, execution, migration, and benchmark tests: 33 tests, OK
Fresh CLI init -> requirement -> invariant validate -> doctor -> projection rebuild -> status: all exited zero
Plugin structure validation: OK
OpenSpec validation: valid
git diff --check: clean
```

The real five-sample benchmark report is `docs/audits/2026-07-11-local-core-slimming-benchmark.json`:

| Metric | Schema 29 baseline | Schema 30 current |
| --- | ---: | ---: |
| Fresh init median | 0.310000 s | 0.087821 s |
| Empty DB | 552,960 bytes | 307,200 bytes |
| 5,000-fact mutation median | 0.146113 s | 0.004391 s |
| Requirement targeted projection | not recorded | 0.002928 s, 3 views |
| Full projection | not recorded | 0.021994 s, 13 views |
| Full unittest | 406.72 s, 370 passed | not-run in this Wave |

Wall-clock values are report-only and are not CI thresholds. The current full-suite field remains explicitly `not-run`; it will be replaced only with the real Task 11.3 result.

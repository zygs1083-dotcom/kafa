# Kafa 28-Finding Final Traceability Matrix

Date: 2026-07-11

Branch: `v1.26-stop-ship-correctness`

Baseline: `docs/audits/2026-07-10-systemic-issues-and-remediation-order.md`

Status: 26 findings closed; 2 findings accepted with explicit fail-closed
boundaries. No finding is marked closed solely from fixture, mock, skipped
live execution, source-string inspection, or model-authored evidence.

## Disposition Rules

- `closed` means the unsafe behavior has a regression, a committed guard, and
  local verification evidence.
- `accepted risk` means the missing host capability is named, its unsupported
  path is blocked or audit-only, and no substitute capability is claimed.
- Connector, worker, Hook, Skill, eval, and native receipt output never becomes
  delivery evidence without Kernel controller verification.
- Repository release state remains `development`; this matrix is remediation
  evidence, not release authorization.

## P0 Findings

| Finding | Disposition | Guard and regression | Commits | Evidence |
| --- | --- | --- | --- | --- |
| DT-001 | closed | Structured open high/critical findings block readiness; resolved findings, waiver expiry, and old-cycle scope are covered in `tests/test_stop_ship_regressions.py` (`test_dt_001_*`). | `fc7c59e`, `20bd1f6` | Wave 1/schema 29 green; final 363-test run |
| DT-002 | closed | Database sequence and explicit supersession define the latest gate; same-second pass then fail is covered by `test_dt_002_same_second_newer_fail_gate_wins` and replay supersession tests. | `fc7c59e` | Wave 1/schema 29 green; final 363-test run |
| CY-001 | closed | Cycle facts have immutable internal identity plus `(cycle_id, local_id)` semantics; local-ID reuse and event replay are covered by stop-ship, schema 29 migration, and schema lifecycle tests. | `800f724`, `fc7c59e` | Wave 1/schema 29 green; final 363-test run |
| TR-001 | closed | Kernel CLI verifies externally supplied connector receipts and cannot self-issue trust; empty-token and legacy downgrade paths are covered by stop-ship, session-attestation, and operating-system tests. | `0149ad2`, `fc48617` | Wave 1/schema 29 green; final 363-test run |
| QS-001 | closed | Quickstart stops before review; `fresh` gates require a distinct reviewer session plus matching attestation at write and delivery-decision boundaries, while local guidance uses `same-context-degraded`. Covered by `test_qs_001_*` and executable cold-start guided-loop tests. | `4177b6d` plus 2026-07-11 local follow-up | Wave 1/schema 29 green; overall-simulation follow-up; final 368-test run |
| IN-001 | closed | User marketplace source resolves to the managed copied plugin; path mismatch and end-to-end isolated install are covered by stop-ship/install tests and real Codex smoke. | `a23d988`, `eedf373` | Wave 3 green; Wave 6 real install; final isolated smoke |

## P1 Findings

| Finding | Disposition | Guard and regression | Commits | Evidence |
| --- | --- | --- | --- | --- |
| DB-001 | closed | Schema scripts require a caller transaction and roll back DDL plus caller facts in file and memory stores; `test_db_001_*` and failure injection cover the contract. | `0f894f0` | Wave 1 schema lifecycle red/green; final 363-test run |
| DB-002 | closed | Migration reads actual version and permits only registered paths; wrong source, unknown target, downgrade, dry-run, markdown, concurrency, and recovery tests fail closed. | `0f894f0`, `800f724`, `fc7c59e` | Wave 1 schema lifecycle and schema 29 green; final 363-test run |
| CN-001 | closed | Notion create forces searchable dual markers; ambiguous success recovers without a second POST and marker miss remains unknown. Covered by `tests/test_notion_ambiguous_recovery.py`. | `094003b` | Wave 4 green; stability E2E 12/12 |
| CN-002 | closed | Linear comment/update fetches and validates remote team/project scope before search or mutation; cross-project, missing metadata, and unknown recovery are covered by `tests/test_linear_scope_isolation.py`. | `68dfe99` | Wave 4 green; final 363-test run |
| CN-003 | closed | Canonical immutable payload hash binds each idempotency key; semantic reuse, conflict, tampering, blank hash, and completed immutability are covered by `tests/test_connector_exactly_once.py`. | `1b053fd` | Wave 4 green; stability exactly-once recovery |
| IN-002 | closed | `kafa project init/status/quickstart` resolves installed runtime outside source repos; quickstart emits executable resolved-runtime commands with actual IDs and legal phase order; unhealthy doctor returns nonzero. | `9cd0e74` plus 2026-07-11 local follow-up | Wave 3 green; overall-simulation follow-up |
| IN-003 | closed | Hooks use installed `PLUGIN_ROOT`, manifest version, session cwd, and valid host JSON; fallback and real Stop Hook behavior are covered by `tests/test_codex_hooks.py`. | `85dbb6a`, `cdb3919` | Wave 3 green; Wave 6 real Hook execution |
| IN-004 | closed | Doctor validates marketplace, installed content, cache identity, Hook definitions, and drift; isolated smoke verifies real app-server discovery of exact Plugin, 12 Skills, and five Hooks. | `63cd7f3`, `eedf373`, `4fa890b` | Wave 3 and Wave 6 green; final isolated smoke/doctor |
| RL-001 | closed | `release.json` aligns version, package, tag, notes, runtime/schema, artifacts, and compatibility prerequisites; conflicting notes and missing live gate fail closed. | `8d2e80e`, `9d7c594`, `2852f2e` | Wave 3 and Wave 6 green; final release contract `ok=true` |
| EV-001 | closed | Explicit live profile returns passed/failed/blocked; disabled is non-success not-run; enabled authenticated run executes real Codex. Covered by live-profile tests and final live report. | `96abb89`, `644c663`, `4fa890b` | Wave 6 green; `/tmp/kafa-final-live.json` 2/2 |
| EV-002 | closed | Fixture success uses public integration and candidate-bound executed validation; validator monkeypatch is forbidden by regression. | `96abb89`, `644c663`, `3efb3fd`, `b36129a` | Wave 6 green; final fixture 5/5 |
| HP-001 | closed | Optional legacy Host worker has watchdog, liveness, deadline, process-tree cancellation, terminal CAS, and late-report rejection; timeout/cancel/dead-worker tests create no evidence. | `4423a6a` | Wave 5 green; final 363-test run |
| HP-002 | closed | Native host policy is authoritative; legacy Host requires explicit isolated deny-all opt-in and rechecks worker/job policy. Covered by permission and tampered-job tests. | `45830f0`, `4423a6a` | Wave 5 green; final 363-test run |
| HP-003 | closed | Native export/import exchanges immutable constraints and real host IDs; import creates raw report/attempt only. Placeholder, hash, drift, exactly-once, policy, and controller verify paths are covered. | `af44971`, `f1c575d`, `4fa890b` | Wave 5 green; Wave 6 real native receipt path |
| ST-001 | accepted risk | Root workspace is the sole mutable SQLite writer; managed worktrees receive immutable packages without DB/runtime/secrets; hosted mutation is unsupported until authenticated Project Fact Transport exists. Tests reject unknown host policy and assert packages exclude runtime state. | `af44971`, `f1c575d` | Native Runtime ADR; Wave 5 green |
| PK-001 | closed | Base package is dependency-free; Host SDK is only `kafa[host-codex]`, and missing optional SDK fails closed. Covered by package metadata, install, and Host provider tests. | `43e58c7` | Wave 3 and Wave 5 green; final isolated base install |

## P2 Findings

| Finding | Disposition | Guard and regression | Commits | Evidence |
| --- | --- | --- | --- | --- |
| HP-004 | closed | `manual-csv` is removed from Provider choices and remains an explicit controller export/import exchange. Covered by `test_manual_csv_is_exchange_not_provider`. | `5459ad8` | Wave 5 green; final 363-test run |
| AR-001 | closed | Explicit `core.api`, one-way Delivery Decision dependencies, extracted Schema Lifecycle and Cycle Ledger, and a real monolith reduction are covered by five Kernel architecture tests. | `54ccc32` | Wave 7 green; 62-test contract group |
| AR-002 | closed | Freeze protects public contracts while permitting internal modules; AST local-import validation rejects deleted referenced modules without freezing filenames. | `7cdc3c7`, `f300232` | Wave 7 green; feature-freeze/install tests |
| AP-001 | accepted risk | Apps/MCP remains the target authorization/execution layer, but public Codex 0.143.0 lacks a result-bound verifiable receipt. Legacy direct results stay audit-only; `test_apps_mcp_receipt_gap_is_explicitly_blocked_not_faked` prevents a fabricated adapter. | `fdf36bb`, `c5cfe50` | Apps/MCP Receipt ADR; Wave 4 and Wave 6 green |
| MR-001 | closed | Native packages emit capability/risk hints without model slugs; host receipt records actual choice. Spark selection remains explicit legacy-only behavior. Covered by route-advice and Host model-policy tests. | `4caf435` | Wave 5 green; final stability matrix |
| DC-001 | closed | Current docs describe schema 29/native-host behavior; chronology stays in CHANGELOG; release notes reject conflicting schema claims. Covered by documentation and release-contract tests. | `2852f2e`, `83f849b` | Wave 7 green; 62-test contract group |

## Final Evidence Matrix

The original assembled implementation at `f300232`, plus the verified
2026-07-11 follow-up working tree based on `a95848b`, produced:

```text
py_compile: exit 0
validate_structure.py: exit 0
unittest discover with ResourceWarning promoted to error: 368 tests in 592.501s, exit 0
runtime smoke: 3/3
forward eval: 3/3 wrapper
skill eval: 18 markers
fixture E2E: 5/5
stability E2E: 12/12
isolated install smoke: codex-cli 0.143.0, passed
live-codex: live_status=passed, 2/2, skipped=0
release contract: ok=true, release_state=development
repo install plus doctor: ok=true
```

The full unit run emitted four unclosed-SQLite `ResourceWarning` messages on
Python 3.14. They are retained as test-hygiene risk, not hidden and not counted
as failed assertions. The real live run did not observe a native child
subagent and used a worktree owned by the eval runner; both non-claims are
recorded in the report.

## Final Decision

All six P0 findings and all P1 findings except the explicitly bounded ST-001
product limitation are closed. All P2 findings are closed except AP-001, whose
required host-verifiable receipt does not exist in the pinned public host
contract. Both accepted risks fail closed and cannot satisfy delivery trust.

The 28-finding remediation goal is complete at the local evidence level. The
repository remains unpushed, unmerged, untagged, unreleased, and undeployed.

# Kafa Local Workflow Lightweighting Issue Matrix

Date: 2026-07-22
Change: `local-workflow-lightweighting`

This matrix uses new `LWL-*` identifiers because the earlier `KAFA-P0-*`
delivery-integrity findings are already closed and must not be redefined.
`openspec/changes/local-workflow-lightweighting/tasks.md` is the only execution
checklist; this file records problem-to-acceptance traceability only.

## P0 — Must Close First

| ID | Problem | Acceptance | State |
| --- | --- | --- | --- |
| LWL-P0-1 | Workflow ownership, route, stage, and gate narration is repeated and has already drifted across docs/Skill/eval. | One versioned presentation contract; deterministic generated/checkable blocks; runtime evaluator remains the only gate authority; both legal submit/verify orderings pass. | closed |
| LWL-P0-2 | `quickstart minimal` is shorter but applies multiple transactions and couples setup with optional execution. | One atomic, idempotent delivery-plan graph; explicit baseline; explicit verified-patch execution; no task/gate/readiness/delivery advancement. | closed |
| LWL-P0-3 | Default quickstart status emits 3,392 bytes, seven issue lines, and eight scaffold commands for an empty initialized project. | Every default is an exact state/blocker/action card with at most one action; initialized-empty quickstart is <=25% of its baseline; complete `--verbose`/`--json`; recovery remains first and fail closed. | closed |
| LWL-P0-4 | Delivery prose repeats normalized facts and caller acceptance text can determine delivery-acceptance links. | Relations and authoritative narrative derive from proven structured facts; prose is supplemental judgment/exception/handoff only; schema 31 unchanged. | closed |

## P1 — Maintenance And Adoption

| ID | Problem | Acceptance | State |
| --- | --- | --- | --- |
| LWL-P1-1 | Ordinary use alternates among `kafa`, internal runtime paths, and Skill proxy paths. | Supported business-project domains are reachable through `kafa project ...`; internal paths are maintainer-only guidance. | closed |
| LWL-P1-2 | Skill/Hook/template/schema/script inventories are hard-coded by several validators and evaluators. | One inspected-plugin distribution manifest feeds every inventory consumer and rejects missing/extra surface. | closed |
| LWL-P1-3 | Full delegation/audit/retrospective/live-host/rehearsal detail is too visible for small single-producer work. | Five-field default packet and closed advanced triggers; deep/schema/trust/gate work remains mandatory and root-owned. | closed |
| LWL-P1-4 | Internal phase/cycle state dominates ordinary status. | Default card hides internals unless actionable; verbose/JSON preserves all facts. | closed |

### P1 Exit Adversarial Findings

| ID | Severity | Finding | Acceptance | State |
| --- | --- | --- | --- | --- |
| LWL-P1-F1 | High | Public `kafa project doctor` can report healthy when the runtime doctor detects foreign-key or other database corruption. | Public doctor reuses the complete runtime doctor, merges wrapper checks, returns nonzero for integrity/schema/invariant/projection failure, and keeps concise/verbose/JSON conclusions consistent. | closed |
| LWL-P1-F2 | High | Runtime files can change after the second digest check but before `subprocess.run()` opens `harness.py`. | Execute only a complete private snapshot of one verified plugin tree; replacement after validation never executes; argv/stdout/stderr/exit behavior remains exact. | closed |
| LWL-P1-F3 | Medium | Deep and advanced triggers are not discoverable from the entry Skill, and the retained root list omits runtime ownership, permissions, concurrency, data loss, and public API. | Generate a compact trigger index into the entry Skill without loading the full matrix; all deep surfaces route to root/deep and the six scenario outcomes are tested end to end. | closed |
| LWL-P1-F4 | Medium | Default Skill eval is labelled passed although it is fixture-only, and a transcript can append a contradictory routing verdict while retaining all markers. | Emit explicit fixture-only versus host-evaluated state; require closed structured scenario verdicts; reject contradictions, missing scenarios, and unknown scenarios. | closed |
| LWL-P1-F5 | Medium | Runtime doctor appends configuration issues before database integrity issues, so concise output can hide corruption behind `.gitignore` noise. | Sort doctor blockers recovery/path first, then integrity/FK, schema/invariant, projection, and configuration; concise selects the highest priority while verbose/JSON retain all. | closed |
| LWL-P1-F6 | Medium | Hook events and Native template candidate exclusions remain hard-coded outside the distribution manifest. | Derive both from the inspected or installed manifest; a synchronized manifest/artifact rename needs no second list and candidate exclusions follow the active names exactly. | closed |
| LWL-P1-F7 | Medium | App Server discovery accepts duplicate or wrong Skill and Hook mappings and does not bind cache plugin metadata to the requested name/version. | Require exact canonical Skill, Hook source/command, plugin name, and version wiring; duplicates and mismatches fail closed. | closed |
| LWL-P1-F8 | Medium | Exact inventory checks ignore undeclared nested files in core and other declared runtime trees. | Enforce a recursive closed inventory; explicitly declare required subtrees such as script fixtures; source, authority, cache, structure, install, and evaluator checks reject nested extras. | closed |
| LWL-P1-F9 | High | Semantic runtime validation can race the first digest, allowing one tree to be validated and another tree to become execution authority. | Bind semantic validation between two equal full-tree digests and reject any tree that changes during validation. | closed |
| LWL-P1-F10 | High | The private runtime snapshot inherits ambient Python startup paths and can import an attacker-controlled `sitecustomize` before the verified entrypoint. | Start the snapshot with isolated/no-site/no-bytecode flags and bootstrap only the verified snapshot roots. | closed |
| LWL-P1-F11 | Medium | A regular file passed as `--repo` raises an unwrapped filesystem exception instead of one deterministic doctor envelope. | Reject non-directory repositories and return the same concise/verbose/JSON error contract without a traceback. | closed |
| LWL-P1-F12 | Medium | Strict doctor parsing accepts contradictory initialized/state pairs, non-finite JSON values, and noncanonical blocker codes. | Enforce a duplicate-safe, finite, exact-shape envelope with closed state/exit/issue relations and blocker grammar. | closed |
| LWL-P1-F13 | Medium | No-SQLite sentinel tests patch only the parent process while the public doctor opens SQLite in a child, leaving the asserted boundary unobserved. | Inject a child-process SQLite audit probe, prove it fires for initialized state, and prove migration/recovery/path sentinels prevent every child SQLite connection. | closed |
| LWL-P1-F14 | Medium | App Server Hook discovery accepts a forged command whose final token merely contains the required event as a substring. | Require exactly interpreter, manifest runner, and exact manifest event tokens. | closed |
| LWL-P1-F15 | Medium | Source and self-contained Hook validators accept wrong interpreters, forged event substrings, and extra command tokens. | Apply the same exact closed command grammar in source, package, and self-contained structure validation. | closed |
| LWL-P1-F16 | Medium | Control-plane and evaluator consumers hard-code the Hook definition filename after the manifest becomes the inventory authority. | Derive the Hook definition and runner paths from the inspected manifest; synchronized manifest/file renames need no secondary list. | closed |

P1 closure evidence: affected combined suites passed 229/229 with
`ResourceWarning` treated as error; two bounded independent re-reviews passed
58/58 and 79/79 with no remaining Critical/High/Medium finding; complete
discovery ran 927 tests with 14 explicit skips and no failure/error; fixture E2E
passed 6/6, stability E2E passed 11/11, runtime smoke passed 2/2, and fresh
Native single/parallel reports both completed with current source/status identity.

## P2 — Release And Evidence Pressure

| ID | Problem | Acceptance | State |
| --- | --- | --- | --- |
| LWL-P2-1 | Real Native evidence is psychologically and operationally treated as universally blocking. | Closed conservative change-scope classifier; Host/package/release/evaluator changes block; unknown blocks; unavailable selected evidence never becomes pass. | closed |
| LWL-P2-2 | Checksum/SBOM/provenance/rehearsal logic and volatile proof bundles dominate review surface. | Shared artifact subject/digest model plus stable summary/digest; all tamper checks retained; required missing details fail. | closed |
| LWL-P2-3 | No field window expands several null/not-run metric objects. | One `field_metrics_status=not-observed` sentinel; observed windows retain complete metric semantics. | closed |

### P2 Exit Adversarial Findings

| ID | Severity | Finding | Acceptance | State |
| --- | --- | --- | --- | --- |
| LWL-P2-F1 | High | Release scope uses the nearest reachable `v*` tag rather than a verified published release and trusts the candidate classifier's partial output, so an intermediate tag or classifier self-tamper can downgrade Host changes to advisory. | Resolve the base from independently published release metadata or fail unknown/blocking; bind exact base/head/path set; validate the complete decision; independently force classifier/workflow self-changes and every blocking scope to Native single+parallel. | closed |
| LWL-P2-F2 | Medium | Stable Native summary verification accepts structurally incomplete or zero-telemetry current/pass detail that the complete evaluator rejects. | Reuse the complete Native report contract; separate historical integrity from current eligibility; blocking current verification independently rechecks source and Native binary, and incomplete/zero/fixture detail fails. | closed |
| LWL-P2-F3 | Medium | Summary `requirement` and `change_scopes` are caller-controlled and are not bound to a classifier decision or candidate. | Bind the summary to the complete versioned decision or its exact digest, derive requirement/profiles from it, and reject Host/packaging/release/evaluator/unknown scopes unless blocking. | closed |
| LWL-P2-F4 | Medium | Supply-chain sidecar/tooling/source reads are path-check-then-open operations, allowing a concurrent replacement to validate transient bytes while corrupt bytes remain. | Use one fd-bound regular-file snapshot for parsing and digesting each input; reject open/read/restore races while preserving all existing artifact, SBOM, checksum, provenance, tooling, and source tamper checks. | closed |
| LWL-P2-F5 | Medium | A minimal self-reported rehearsal detail can become current/passed without doctor, migration, backup, Hook, plugin digest, uninstall, or exact artifact checks. | One shared rehearsal report validator must enforce the complete artifact-mode smoke, subject/digest, fixed steps, source/build/Syft, user-state, invariant, and no-external-effect contract. | closed |
| LWL-P2-F6 | Medium | Outcome validation does not rederive numerator, denominator, window, benchmark status, or summary, so four passing scenarios can coexist with fabricated zero metrics. | Recompute every aggregate and window fact from scenarios for v1 and v2; validate status/command/returncode/time semantics; retain v1 compatibility and v2 `not-observed` without false zeros. | closed |
| LWL-P2-F7 | Medium | The captured Native validator entrypoint can import a concurrently replaced dependency; a committed link can escape the current clone, and unsafe cleanup can follow a substituted path. | Run entrypoint and dependencies from one private clean-HEAD or descriptor-copied snapshot; reject link/reparse/special source; seal and identity-check every source file and parent directory; restore permissions only through unchanged identities. | closed |

P2 closure evidence: the final affected evidence/documentation suites passed
48/48 and the final combined classifier, evidence, evaluator, artifact,
supply-chain, rehearsal, outcome, release, documentation, and install gate
passed 209/209 with `ResourceWarning` treated as error and no failure, error,
skip, or expected failure. The F7 red/green set covers mutable original
dependencies, private-snapshot replace-and-restore, committed source links, and
cleanup path substitution without changing repository-external permissions.

## Final Independent QA Findings

| ID | Severity | Finding | Acceptance | State |
| --- | --- | --- | --- | --- |
| LWL-FINAL-F1 | High | The generated happy path used `T1`/`Q1` instead of the plan's `PATCH-T1`/`PATCH-Q1`, and submitted the generated task without first transitioning it from `planned` to `active`. | Add a canonical `task-start` stage and dependencies, use the actual generated IDs, and execute the generated command chain through start, verification, submit, accept, and gate in a real temporary project. | closed |
| LWL-FINAL-F2 | Medium | A readable but structurally incomplete SQLite database caused status, doctor, and quickstart JSON modes to emit no JSON and leak a traceback. | Convert expected schema/read failures into the shared fail-closed envelope; all three JSON commands return exactly one object, no traceback, no initialization advice, and a nonzero exit. | closed |
| LWL-FINAL-F3 | Medium | The presentation dependency graph did not require baseline confirmation before delivery readiness. | Add `baseline-confirmation -> delivery-readiness` and reject the inverse ordering in a deterministic contract test. | closed |

Final-QA closure evidence: the three deterministic red contracts failed before
production edits and passed 4/4 after the fix; the affected workflow,
documentation, operator, cold-start, delivery-plan, local-core, Hook, and
feature-freeze gate passed 135/135. The bounded re-review passed 28/28 with zero
open Critical/High/Medium finding. The final complete discovery then ran 974
tests: 960 passed, 14 remained explicitly skipped, and zero failed or errored.

## Stop Conditions

- No P1 production edit starts before all LWL-P0 items have current red/green,
  regression, benchmark, and adversarial-review evidence.
- No P2 production edit starts before all LWL-P1 items pass the same gate.
- Critical, High, or Medium findings reopen the affected item.
- Skipped, blocked, not-run, fixture-only, zero-test, advisory, or historical
  evidence never closes an item as passed.

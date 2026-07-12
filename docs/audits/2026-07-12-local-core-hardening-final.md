# Local Core Hardening Final Audit

## Audit identity and disposition

- OpenSpec change: `local-core-hardening`
- Parent change: `local-core-slimming`
- Candidate branch: `v2-local-core-slimming`
- Baseline `main` and `origin/main`:
  `adba3691d859c0ffc93d75cc148d8f916314cc49`
- Native evaluation `HEAD`:
  `2ac40a3e79ab2977a8ba3c841f0ed693ef25fd1f`
- Executable workspace SHA-256:
  `68dbed438a91b81f3a8d5ca8c3cbd3846cedf7b3207c63ab8bff2d737cbda6ee`
- Executable status SHA-256:
  `40852112f38c89647ca561b8215abbae2863d05169a702a982094ca6ae7e832b`
- Executable status entries: 12
- Schema/runtime: 30 / 5.0.0
- Candidate source release: `2.0.0-beta.1` / `2.0.0b1`
- Native evaluation state: intentionally uncommitted and dirty with an empty
  cached diff. Those Git fields are historical run metadata; publication
  changes HEAD/status without changing the bound executable bytes.

The hardening change is complete for the authorized local scope. It closes the
three original stop-ship findings, seven High and five Medium adversarial or
publication-QA findings, and the cross-platform defects exposed by the first CI
matrix. It adds no table, command, Skill, Hook, template, network
dependency, Connector, or Host lifecycle. The source remains a local-only
verified delivery kernel.

The quality conclusion in
`docs/audits/2026-07-12-local-core-slimming-final.md` is superseded where it
claimed the pre-hardening candidate had complete migration and trust coverage.
That audit remains the historical slimming and accepted-LOC record; this audit
is the current authority for migration concurrency, projection rollback, and
review-status behavior.

## Finding closure

### Original hardening findings

| ID | Original defect | Closed behavior |
| --- | --- | --- |
| HC-MIG-1 | Store operations could commit between the last migration fingerprint and `os.replace` | Every file-backed connection, transaction, and backup now holds the same five-second, same-thread-reentrant project operation lock. Migration announces through the sentinel before acquiring that lock, waits for earlier work, then rereads and backs up the committed state. New work fails before SQLite opens. |
| HC-PROJ-1 | A post-activation failure restored SQLite but could leave schema-30 projections live | Migration backs up all 14 possible filesystem side effects, including 13 live projections and the retired evidence view. Final doctor runs before live publication; later failure restores verified DB bytes and exact projection bytes/modes, removes newly created views, and reports `rollback-incomplete` if projection restoration fails. |
| HC-TRUST-1 | Distinct-looking IDs could promote `same-context-degraded` high-risk review into accepted-risk | `review_status` is a required keyword-only trust input. High/critical accepted-risk requires exact `reviewed-local`, distinct non-empty context metadata, current structured execution, and complete current unexpired accepted/exempt metadata for every remaining risk. Degraded review remains `human-review-required`. |

The deterministic initial red checkpoint ran eight new tests. All eight failed
for the intended three defect families while five surrounding positive
migration and trust cases remained green. The same eight tests passed after the
production changes.

### Findings discovered during adversarial remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| High | Schema 27/28/29 migration could use a legacy session ID as if it were producer/reviewer context | Migration now copies only real, non-empty context metadata. Missing or shared context remains empty and `same-context-degraded`; session identity never promotes review. |
| High | Whitespace, case variants, or unknown `review_status` could cross a trust branch inconsistently | Only canonical `reviewed-local` and `same-context-degraded` values are accepted. Every noncanonical value fails closed; JSON schema enums match runtime behavior. |
| High | Active schema-30 REAL revision values such as `1.9` could be truncated with `int()` and appear current | Runtime revision parsing rejects non-integral REAL/TEXT/negative values across project, gate, accepted-risk, and finding checks. |
| High | Schema 27/28/29 migration could truncate or clamp malformed project/gate revisions and make stale risk acceptance current | Project, task, and gate revisions now require exact positive SQLite integers; fractional, textual, zero, and negative values fail staging with no activatable database. |
| High | A complete accepted high/critical finding was treated as nonblocking without making the trust path high-risk | Every unresolved or accepted high/critical finding now contributes a named risk; accepted metadata is revalidated, structured execution is required, degraded review remains blocked, and reviewed-local delivery is labeled `accepted-risk`. |
| High | Fractional legacy `exit_code`, `executed_count`, or policy flags could be coerced into immutable schema-30 execution evidence | Migration now accepts only exact SQLite integers/flags; malformed evidence is ineligible and its validation is invalidated, while malformed target flags abort staging. |
| High | Active delivery evaluation could similarly coerce fractional execution or target metadata after direct DB tampering | Delivery evaluation independently checks exact gateable/sandbox/no-network flags, exact zero exit code, and positive integer execution count before trusting an immutable execution. |
| Medium | Invalidated legacy validations could lose a valid `superseded_by` chain | Schema 27/29 migration retains valid supersession edges while invalidating unauthoritative judgments. The published schema-28 fixture has no source `validations` table and therefore no chain to preserve. |
| Medium | Unknown low/medium review state could be promoted to `reviewed-local` | Unknown state fails closed; canonical low/medium degraded review remains allowed only with the explicit degraded label. |
| Medium | Delivery status did not persist/project the exact `same-context-degraded` or `accepted-risk` result | Delivery facts and Markdown projections now retain the evaluated decision status; `delivery.schema.json` and `quality-gate.schema.json` use exact enums. |
| Medium | A committed Native report could never keep matching the current `HEAD/status`, because committing the report necessarily changes both | Report generation and `should_fail` keep strict current-Git validation. Persisted evidence may retain its historical Git metadata, but current executable bytes and source scope must still match exactly. Time must be timezone-aware ISO-8601 and Git identity must be a nonzero object-ID shape; source-digest changes remain blocking. |
| Medium | Packaged Skill/template instructions implied that risk acceptance could waive structured execution or independent review prerequisites | All packaged operating instructions now require structured current-candidate execution, exact `reviewed-local`, and distinct non-empty contexts first; acceptance covers only each named remaining risk. |

Eight additional adversarial test methods produced 13 expected assertion
failures plus one schema-contract `KeyError` before these corrections. All
eight passed after remediation. No negative trust test was removed or relaxed.

The publication-boundary regression first failed because the persisted-report
mode did not exist, then the existing documentation contract failed on the old
workspace digest after the focused fix changed executable source. After new
real Native single and parallel runs, the focused report contract passed 2/2,
the evaluator module passed, and that pre-push suite passed 296/296.

Fresh publication QA then added six deterministic red methods. They produced
19 intended assertion failures and five intended error paths across revision
laundering, accepted-finding review, migrated command evidence, active runtime
tampering, and instruction drift. After remediation, all cases were green and
the complete strict suite passed 303/303. No negative trust or migration test
was weakened or removed.

## Migration and recovery evidence

The verified ordering is:

1. create `local-core-migration.lock` atomically;
2. acquire `.ai-team/state/harness.db.operation.lock`;
3. checkpoint WAL, reread source, and create the verified DB backup;
4. back up and hash all 14 bounded projection paths;
5. build staging, verify fingerprint, and replace the active DB;
6. run schema/FK/domain doctor;
7. render and verify all 13 live projections;
8. on failure, restore the verified DB and exact projection state before
   releasing the operation lock;
9. release the operation lock, then remove the sentinel.

Multiprocessing Event/Pipe tests, not timing sleeps, prove that an already
active writer finishes and is included, while reads/writes starting after the
sentinel fail closed. Success, injected exceptions, and process exit release
the OS lock. A POSIX fork probe proves a child cannot inherit the parent's
reentrant entitlement and can acquire only after the parent releases it.
`kafa project doctor` returns `migration-in-progress` without opening SQLite
when the sentinel exists.

Failure injection covers five migration points, final-doctor failure, partial
projection publication, DB rollback, projection restore, and an incomplete
projection restore. Exact bytes, modes, SHA-256 values, absence/presence state,
backup manifest paths, and schema 27/28/29 facts are checked. No operation lock,
sentinel, partial, restore, or temporary database remains after handled runs.

Independent migration QA reran 60 strict operation-lock, schema migration,
schema lifecycle, backup, store, and doctor tests plus three read-only
projection probes. It found no remaining Critical, High, or Medium finding and
changed no file.

## Delivery-trust and ownership evidence

The trust matrix covers exact review-status propagation, current candidate and
revision binding, structured execution, immutable execution rows, complete
accepted/exempt risk metadata, expiry, waiver text, same-context spoofing,
dirty Git, malformed SQLite values, and direct SQL tampering. Execution UPDATE
and DELETE are rejected by database triggers. Invalid or stale provenance does
not become a synthetic receipt.

Low/medium `same-context-degraded` remains an honest local delivery result and
is persisted and projected with that label. High/critical degraded review
remains `human-review-required` even with distinct-looking IDs and complete
risk acceptance. Only a canonical fresh distinct `reviewed-local` gate can
enter the procedural `accepted-risk` path.

Independent trust QA reran the complete 27-test delivery-policy suite, five
focused caller/delta tests, three original exploit probes, and an AST audit of
all 13 `evaluate_local_trust` call sites. It found no remaining Critical, High,
or Medium finding and changed no file.

Native Codex/ChatGPT remains sole owner of task execution, subagents,
worktrees, approvals, model selection, cancellation, and handoff. Kafa owns
only local facts, projections, validation, and delivery decisions. Active
runtime scans and structure tests find no GitHub, Linear, Notion, Figma, Slack,
Connector-token, `gh api`, Host SDK worker, provider, or fabricated-receipt
execution path.

## Regression and local E2E

All counts below are executable passes; skipped, expected-failure, blocked,
not-run, or fixture-only results are not included as substitutes for another
profile.

| Gate | Result |
| --- | --- |
| Final affected strict matrix | 73/73, `ResourceWarning` promoted to error |
| Complete strict unittest discovery | 303/303, 82.569 s internal / 82.76 s wall, no skip or expected failure |
| Runtime smoke | 2/2 |
| Skill evaluation | 17/17 required markers |
| Fixture E2E | 6/6; zero skip, false-pass, or SQLite-lock errors |
| Stability E2E | 11/11; zero skip, false-pass, or SQLite-lock errors |
| Final migration QA | 60/60 plus three read-only projection probes |
| Final trust QA | 27/27 plus 5/5 caller/delta tests and three exploit probes |

Python compilation, release and plugin structure validation, all 25 JSON
documents, both OpenSpec changes, both Native report consistency checks, and
`git diff --check` all passed again at the final checkpoint.
The hardening benchmark is persisted in
`docs/audits/2026-07-12-local-core-hardening-benchmark.json`.

## Before/after performance and scale

The hardening measurements are compared first with the pre-hardening schema-30
candidate because that isolates the cost of the safety changes. The older
schema-29 baseline is included for the like-for-like mutation and init budget
context.

| Metric | Schema 29 baseline | Pre-hardening schema 30 | Hardened schema 30 | Hardening delta |
| --- | ---: | ---: | ---: | ---: |
| Fresh DB | 552,960 B | 315,392 B | 315,392 B | 0 B |
| Fresh init median | 0.310000 s | 0.114920 s | 0.098336 s | -14.43% |
| One mutation after 5k facts | 0.146113 s | 0.004390 s | 0.005152 s | +17.36% |
| Full 13-projection median | not recorded | 0.021977 s | 0.026044 s | +18.51% |
| Full strict suite | 370 / 406.72 s | 258 / 82.99 s | 303 / 82.76 s | +45 tests; timings not workload-equivalent |
| Total Python LOC | 33,521 | 23,927 | 27,107 | +3,180 / +13.29% |
| Test Python LOC | 13,251 | 8,940 | 11,045 | +2,105 / +23.55% |
| Plugin Python LOC | 18,878 | 12,971 | 13,934 | +963 / +7.42% |

The 5k mutation median is 0.005152 seconds, 89.7% below the mandatory
0.050-second ceiling. Full projection remains 0.026044 seconds. The safety
tests and implementation increase LOC relative to the slimmer candidate, but
the user-approved original slimming deviation remains unchanged in status: the
35%-45% total/test reduction target was not met and is not relabeled as a pass.

## Real Native Host and multi-agent evidence

Both real reports are bound to the executable source identity above and to
Codex binary SHA-256
`d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.
Independent recomputation returned no validator error for source, binary,
scope, tokens, integration, or timing.

The persisted report contract distinguishes executable identity from Git
publication metadata. Source digest and scope must still match the current
checkout byte-for-byte. The recorded HEAD, dirty flag, status digest, and entry
count remain the historical run state after a later commit instead of creating
an impossible self-referential commit hash.

| Profile | Work | Tokens | Controller wall | Native producer wall | Verification |
| --- | --- | ---: | ---: | ---: | --- |
| Single | one isolated producer; only `candidate.py` changed | 49,248 | 36.518 s | 35.426 s | one targeted controller check, rc=0 |
| Parallel | two isolated producers; only `alpha.py` and `beta.py` changed | 99,706 | 39.541 s | 36.983 s; 30.275 s overlap | two targeted plus one combined check, all rc=0 |

Two sequential single units project to 73.036 controller seconds, so the
parallel profile reduces latency by 45.86% (`1.847x`) for this disjoint task.
The parallel average is 49,853 tokens per unit, 1.23% above this single run,
and repeated profiles have shown material run-to-run token variance. This does
not establish a token saving. The evidence therefore supports one producer or a
shared-context batch as the token-conservative default, and parallel fan-out
only for ready, disjoint work with deterministic tests and a latency SLA. It
does not support a claim that multiple models lower token use by themselves.
Actual model identity and monetary cost are not exposed and are not inferred.

## Artifact and installation evidence

Real source and wheel artifacts were built from the hardened candidate:

- wheel SHA-256:
  `c3e701cef3bca7a46b35bc6fd38bdf4996533e59399f09069dbcd6178309dea0`
- source archive SHA-256:
  `c93841e44711557a53c2e5df20db6d23560543f41c1a606b8e9fdeb407f72df2`

The archive was built from the frozen executable candidate immediately before
this audit text and task checkboxes were finalized. Those later documentation-
only edits do not change the executable identity bound by the Native reports;
the archive hash is not presented as a self-referential final-commit hash.

Each artifact passed installation in a temporary venv and temporary HOME. The
installed payload had exactly 7 Skills, 3 Hooks, 3 templates, 16 schemas, and
7 runtime scripts. Plugin installation, Codex app-server discovery, cache
digest, schema-30 quickstart, candidate doctor, Hook execution, uninstall, and
retired-runtime absence all passed. Temporary artifacts and environments were
removed.

The active user installation was never replaced. Final live inspection still
reports global `kafa` and enabled `codex-project-harness@personal` as
`1.25.0-beta.1`.

## CI and authorization truth

`.github/workflows/validate.yml` places the migration-concurrency and trust
targeted suites before the full suite and isolated install in Ubuntu, macOS,
and Windows jobs. At this audit's local checkpoint, commit/push authorization
had not yet been granted, so the recorded pre-publication statuses were:

| Remote gate | Status at local audit checkpoint |
| --- | --- |
| Ubuntu GitHub Actions | `not-run` |
| macOS GitHub Actions | `not-run` |
| Windows GitHub Actions | `not-run` |

These are not passes. In particular, Windows `msvcrt.locking`, open-handle, and
replace behavior has static coverage and a configured CI path, but no Windows
runtime evidence in this delivery.

### Publication follow-up

After the user authorized publication, PR #14's first remote matrix exposed three
additional portability defects rather than converting `not-run` into a pass:

- Windows rejected `os.fsync()` on read-only file descriptors during the
  verified backup path. The backup and migration file helpers now open existing
  files update-capable before flushing; a regression locks this descriptor
  contract. The resulting migration barrier timeouts were secondary failures.
- Windows represented the same temporary directory once through its 8.3 alias
  and once through its canonical user name. The sentinel regression now checks
  the actionable error code, sentinel filename, PID, and operator instruction
  instead of requiring lexical path identity.
- Ubuntu correctly could not resolve the macOS Codex binary path recorded by a
  historical Native report. Cross-host persisted validation now verifies the
  binary metadata and digest shape without claiming that the historical binary
  is installed locally; source digest and scope remain mandatory.

The portability and publication-QA fixes passed 303/303 local tests and
regenerated both Native reports against the final executable digest above.
Remote follow-up results remain PR-owned
evidence and are not backfilled into the pre-publication table.

## Residual risks and explicit boundaries

- Windows operation locking and replacement require the authorization-gated
  remote Windows run before they can be called platform-verified.
- Context IDs, SQLite facts, accepted-risk metadata, and Markdown projections
  are procedural local records, not cryptographic provenance. A principal with
  arbitrary file-write access can modify the DB or triggers. This is an
  explicit OpenSpec non-goal; high/critical ambiguity still fails closed.
- The real Native reports prove local capability and report consistency, not
  trusted delivery provenance. Their trust label remains
  `local-capability-only-not-delivery-provenance`.
- The hardened candidate adds safety code and tests to the already accepted LOC
  deviation. No supported migration, rollback, or trust coverage was deleted
  to improve the metric.
- The Native run captured an uncommitted candidate; a later commit may publish
  the same bound executable bytes while retaining that run metadata. The
  enabled user installation remains the old version and is not replaced by Git
  publication.

Within those declared limits, both independent QA reviews passed with no
Critical, High, or Medium finding, all authorized local verification gates are
green, and the three original stop-ship defects are closed.

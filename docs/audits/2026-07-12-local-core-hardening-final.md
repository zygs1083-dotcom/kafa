# Local Core Hardening Final Audit

## Audit identity and disposition

- Change: `local-core-hardening`; parent: `local-core-slimming`.
- Branch: `v2-local-core-slimming`.
- First publication HEAD: `cff9f4a2483405a76d72c19941ba4aa0c9bcb0d7`.
- Verified implementation HEAD:
  `cab7c7d800c5e0167f8fa8b616b2f424d8b5e0cd`.
- Baseline `main` / `origin/main` at implementation start:
  `adba3691d859c0ffc93d75cc148d8f916314cc49`.
- Runtime / kernel / schema: `5.0.0` / `5.0.0` / `30`.
- Source candidate: `2.0.0-beta.1` (`2.0.0b1`, development state).
- OpenSpec checklist after the verified implementation matrices: 186 checked,
  0 open, 186 total.

All implementation and publication checkpoints are complete. Each checkpoint
closed only after fresh QA plus the exact implementation revision's complete
push and pull-request Ubuntu/macOS/Windows matrices succeeded; old, skipped,
cancelled, fixture-only, or not-run evidence was not reused.

The earlier quality conclusion in
`docs/audits/2026-07-12-local-core-slimming-final.md` is superseded for
migration concurrency, DB/projection recovery, trust evaluation, candidate
identity, Native evidence, and artifact completeness. That document remains the
historical slimming and user-accepted LOC-deviation record.

## Delivered behavior

The candidate preserves schema 30 and the local-only kernel boundary:

- every file-backed Store connection, transaction, backup, migration, repair,
  and production projection publication uses one cross-platform, same-thread
  reentrant project operation lock;
- migration announces with a durable sentinel before waiting, fails new
  operations closed, rereads authority after acquiring the lock, and keeps the
  lock through success or complete rollback;
- migration backup and recovery cover all 13 generated projection paths with
  original existence, bytes, mode, and SHA-256, as well as the verified DB;
- callback self-report is not trusted: core checks DB immutability, reruns doctor,
  independently renders expected projection bytes, and compares every live path;
- WAL/SHM, hard exit, cancellation, restore failure, and interrupted recovery
  retain actionable `recovery-required` or `rollback-incomplete` state;
- `evaluate_local_trust()` requires explicit `review_status`; high/critical
  accepted-risk still requires structured current-candidate execution,
  `reviewed-local`, distinct non-empty producer/reviewer contexts, and complete
  current unexpired metadata for every residual risk;
- production and Native source identity fail closed on Git replacement objects,
  redirected worktrees, missing objects, gitlinks, symlinks, unmerged entries,
  unexpected SQLite tables, and transient controller-source replacement;
- real Native controller commands execute from a verified private Git-backed
  snapshot captured at profile start;
- the sdist now contains one installable release root with `release.json`,
  `VERSION`, and the complete plugin bundle; CI builds real wheel/sdist artifacts
  before isolated installation on each platform.

No Connector, provider worker, Connector token, direct SaaS API, Host SDK
lifecycle, fabricated receipt, new business table, new schema version, new CLI
domain, new Skill, new Hook, or new agent template was introduced. Native
Codex/ChatGPT remains the only owner of task/thread, subagent, worktree,
approval, model, cancellation, steering, and handoff lifecycle.

## Red/green finding closure

| Finding | Red evidence | Closed behavior |
| --- | --- | --- |
| Migration TOCTOU | An active writer could commit between fingerprint and replace | Deterministic process barriers prove the active writer finishes first and is included; operations beginning after announcement fail closed before SQLite opens. |
| Projection split rollback | Activation failure could restore DB but leave schema-30 views | DB and all generated views restore atomically to verified pre-migration state; incomplete recovery remains fail closed. |
| High-risk degraded review | Distinct-looking IDs plus accepted risk could bypass independent review | `same-context-degraded` can never satisfy high/critical independent review. |
| Callback authority bypass | A callback could no-op or inject doctor-valid DB facts | Core independently rejects missing views, projection byte mismatch, and every callback DB fingerprint change. |
| Source/report identity bypass | Git config, replace refs, transient source, coercive JSON types, or hidden SQLite tables could overstate Native evidence | Identity is isolated and pinned; reports are closed, exact-type contracts; private snapshot bytes and exact table inventory are verified. |
| Source artifact incomplete | A PEP 517 sdist omitted `release.json`, `VERSION`, and the plugin, causing artifact-mode install to fail | `MANIFEST.in` defines the release root; the same real wheel/sdist pair passes isolated venv/HOME install. |
| Windows LF/object fixture portability | The first new-HEAD Windows run exposed CRLF projection bytes, a post-commit fixture rewrite that fabricated dirty source, and read-only loose Git objects that the negative tests could not delete | Production projections publish explicit UTF-8 LF bytes; the delivery fixture writes exact LF bytes; missing-object tests add the user-write bit only after Windows returns `PermissionError`. |

The artifact defect was found only because the final plan required a real source
artifact rather than a copied checkout. A deterministic manifest test was red
before `MANIFEST.in` and green afterward. No negative trust, migration, or
delivery test was removed or relaxed.

## Local validation

Every row below is an executed pass. No skipped, blocked, not-run,
expected-failure, fixture-only, or zero-test result is substituted for a
different profile.

| Gate | Current result |
| --- | --- |
| Complete strict unittest discovery | 375/375 in 135.897 s internal / 136.17 s wall; `ResourceWarning` promoted to error; no skip or expected-failure summary |
| Install/release targeted suites | 47/47 |
| Runtime smoke | 2/2; 15 lifecycle commands rc=0; directed/full invariant ratio 48.0696 vs 10x minimum |
| Skill evaluation | 17/17 ordered markers |
| Fixture E2E | 6/6 in 3.796888 s; skipped=0, false-pass=0, SQLite-lock=0 |
| Stability E2E | 11/11 in 6.155027 s; skipped=0, false-pass=0, SQLite-lock=0 |
| OpenSpec | `local-core-slimming` and `local-core-hardening` both 4/4 artifacts and valid |
| Structure/release | plugin structure valid; source release contract valid |
| Repo-scoped source doctor | pass after `--help`, `--dry-run`, temporary repo install, and cleanup |
| JSON/YAML | release, plugin, hooks, 16 schemas, runtime reports, benchmark, and workflow syntax valid |
| Native report consistency | both persistent reports return no consistency error against current executable source |
| Diff hygiene | secret-pattern scan empty; `git diff --check` pass |

The Kafa source repository intentionally has no `.ai-team` runtime. Therefore
`harness.py --root . validate --delivery` reports `harness is not initialized`.
The repository was not initialized merely to manufacture a handoff record;
OpenSpec `tasks.md`, the test evidence, independent QA, PR checks, and this audit
are the delivery authorities for this Kafa-source change.

## Real Native Codex evidence

Both compact reports bind:

- executable workspace SHA-256:
  `afce24b9c482cac40f61939feb033602c9b6f4e2c385c06814ddb611fd7879b1`;
- status SHA-256:
  `2793a33d0f29c83dd1b29cceab727ca6e2966c688a3bd70fe7c06217fb15fb93`;
- status entries: 3;
- Codex CLI: `0.143.0`;
- Native binary SHA-256:
  `d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`.

| Profile | Result | Tokens | Native runtime | Controller verification |
| --- | --- | ---: | ---: | --- |
| Single | passed; only `candidate.py` integrated | 50,370 | 26.741738 s | one structured target, rc=0 |
| Parallel | passed; disjoint `alpha.py` / `beta.py` producers | 99,794 | 21.281547 s; 20.329179 s producer overlap | two targeted plus combined verification, all rc=0 |

The parallel run demonstrates latency reduction for two disjoint ready tasks,
not a universal token reduction. Its per-unit token use is essentially the same
as single and duplicates substantial input context. The conservative default is
single/shared-context work; parallel fan-out is appropriate only for independent,
bounded tasks with deterministic integration checks. Host-selected model identity
and monetary cost are not exposed, so neither is inferred.

## Performance and size

| Metric | Schema-29 baseline | Slim schema-30 checkpoint | Hardened candidate | Result |
| --- | ---: | ---: | ---: | --- |
| Runtime tables | 54 | 27 | 27 | exact |
| Fresh DB | 552,960 B | 315,392 B | 315,392 B | within 320 KiB budget |
| Plugin payload, caches excluded | 1,276 KiB | 752 KiB | 856 KiB | within 1.0 MiB budget |
| Fresh init median | 0.310000 s | 0.114920 s | 0.098193 s | pass |
| One mutation after 5k facts | 0.146113 s | 0.004390 s | 0.004502 s | pass vs 0.050 s budget |
| Full 13-view projection median | not recorded | 0.021977 s | 0.024243 s | measured, not thresholded |
| Strict full suite | 370 / 406.72 s | 258 / 82.99 s | 375 / 135.897 s | pass vs 300 s local reference budget |
| In-scope Python LOC | 33,521 | 23,927 | 32,794 | 2.17% below baseline |
| Test Python LOC | 13,251 | 8,940 | 14,750 | 11.31% above baseline |
| Plugin Python LOC | 18,878 | 12,971 | 15,903 | 15.76% below baseline |

The safety and adversarial coverage materially increased code and tests after
the slimming checkpoint. The locked 35%-45% Python/test LOC target remains a
deviation; the user explicitly accepted that LOC deviation and authorized
closing slimming task 11.16. This audit preserves it as an accepted deviation,
not a metric pass. Plugin size is measured from a same-filesystem copy excluding
`__pycache__` and bytecode, matching the locked method; the apparent 1.42 MiB
working-directory size was cache pollution, not payload.

## Artifact and installation evidence

The final local artifact run used a real PEP 517 wheel and sdist in a temporary
venv and temporary HOME:

- wheel SHA-256:
  `13ebbb2d5b4e4eeae960c9dffd5038a21a111174348c158466415d2a07a78a8c`;
- sdist SHA-256:
  `0258639b2391647d7ca59fc0898224072bb5458ed65e1b6d8b05b6ffdd26f6a7`.

It verified wheel import isolation, marketplace discovery, Codex app-server
discovery, exact 7 Skills / 3 Hooks / 3 templates / 16 schemas / 7 runtime
scripts, cache identity, schema-30 init, quickstart, doctor, Hook execution,
retired-surface absence, and uninstall. Temporary artifacts, HOME, venv,
repo-scoped marketplace, and Python caches are removed before commit.

The active user installation was not overwritten. The PATH `kafa` remains
`1.25.0-beta.1`; the source candidate reports `2.0.0-beta.1` only through
`python3 -m kafa.cli` and isolated artifacts.

## Independent QA

Three independent read-only reviews passed with no open Critical, High, or
Medium finding:

- migration/recovery: all eight reviewed production file hashes remained equal
  to the previously adversarially reviewed revision; combined production SHA
  `4e4ade10777b6862fb6e5729d2014710ff478e92c70c157d8946aa407d29e201`;
  an independent strict rerun passed 375/375 in 142.032 s;
- trust/source/Native: reviewed production and test hashes were unchanged;
  both persistent reports returned `[]` with `should_fail=False`; an independent
  strict rerun passed 375/375 in 139.024 s;
- artifact/CI: install/release tests passed 47/47, an independent temporary-copy
  real wheel/sdist artifact smoke passed end to end, and workflow YAML/contract
  validation passed. The initial unpinned-build Low finding was closed by fixing
  the verified build tool version to `build==1.5.1`.

The remaining Low observation is that the workflow contract test checks the
build command generically rather than asserting the exact pin. The implementation
is pinned and the risk is future regression only; avoiding a new test-only
source-identity churn and another 150k-token pair of real Native reruns is the
more token-conservative final-candidate choice. Remote CI still provides the
required executable artifact proof.

Any production change after these QA identities invalidates the corresponding
review. The final pushed revision must pass both complete three-platform
matrices before merge.

After the first Windows matrix exposed the newline/object-fixture failures, the
same independent reviewers rechecked the incremental correction:

- migration/projection QA confirmed explicit binary publication preserves the
  same truncate/write lifecycle, outer operation lock, file mode, key ordering,
  trailing newline, merge behavior, and exact UTF-8 LF bytes; PASS;
- trust/source QA reran nine replace-ref, missing-object, gitlink, worktree, and
  Native identity cases; the writable-bit retry catches only `PermissionError`
  and does not weaken any fail-closed assertion; 9/9 PASS;
- both latest Native reports bind source `afce24b9...` and pass strict current
  source/binary/Git/matrix validation.

One Low coverage observation remains: the permanent CRLF byte assertion names
project-state explicitly rather than adding a second standalone assertion for a
Markdown view. The shared `write_view` implementation and an independent direct
mode/LF probe cover the latter, and the new Windows full suite is the required
platform proof.

## Remote CI and publication truth

PR: [#14](https://github.com/zygs1083-dotcom/kafa/pull/14).

The first publication revision `cff9f4a` completed both Ubuntu and both macOS
jobs successfully, including real artifact build/install. Both Windows jobs
failed the hardening target with four newline/read-only-fixture findings. The
second revision `586929c` again passed both Ubuntu/macOS matrices but Windows
retained one deterministic dirty-fixture failure: its temporary repository had
inherited global `core.autocrlf=true` while production identity correctly
isolated global config. The third revision `b8d325f` passed Windows hardening and
artifact installation, then the complete Windows suite exposed six remaining
Native/execution/runtime-smoke fixture portability cases. These failures are red
evidence, not passes. The current fully self-contained fixtures require two
entirely new matrices.

| Verified implementation `cab7c7d` | Push matrix | Pull-request matrix |
| --- | --- | --- |
| Ubuntu | success, run `29202221018` | success, run `29202222074` |
| macOS | success, run `29202221018` | success, run `29202222074` |
| Windows | success, run `29202221018` | success, run `29202222074` |

The validate workflow runs hardening targets, complete strict regression,
fixture/stability evidence, and real artifact-mode install on the matrix. All
six jobs for the exact implementation revision completed successfully. The
docs-only closure commit that records this result must also complete both
matrices before PR merge; its result is recorded in the final handoff rather
than creating an infinite evidence-only commit cycle.

## Residual boundaries

- Windows `msvcrt`, open-handle rollback, path, CRLF, real artifact install, and
  the complete 375-test suite are platform-verified for `cab7c7d`.
- Local context IDs, accepted-risk metadata, and SQLite facts are procedural
  records, not cryptographic provenance; ambiguous high/critical delivery remains
  `human-review-required`.
- Real Native reports prove local capability and internal consistency, not
  independent delivery provenance.
- The accepted LOC deviation remains explicit.
- No tag, release, deploy, production migration, secret change, or user-plugin
  replacement is authorized or performed.

At this checkpoint implementation, local validation, independent QA, checklist,
and both three-platform matrices are green. Merge remains blocked only until
the docs-only closure HEAD repeats both complete matrices successfully.

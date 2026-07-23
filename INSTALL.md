# Installation

Codex Project Harness is distributed as a local Git/Codex plugin bundle. The `kafa` helper CLI provides repeatable local marketplace setup; it does not publish to PyPI and does not mutate Codex plugin caches directly.

`release.json` is the source/tag/package contract. A source checkout marked `release_state=development` is not a published release even when its `VERSION` is newer than the latest GitHub tag. Use `python3 -m kafa.release --json` to inspect that distinction.

The business-project runtime is local-only. Installation and normal delivery do not require project-management credentials, remote service tokens, or an optional model SDK.

## Requirements

- Python 3.11 or newer.
- Git on `PATH` for local repository and candidate identity checks.
- Codex with plugin marketplace support.
- A checkout of this repository.

## Validate the source checkout

The internal scripts in this section are maintainer-only source-checkout
validation tools, not business-project runtime entrypoints. Run them from the
Kafa repository root:

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py \
  plugins/codex-project-harness
python3 -m pip install -e .
kafa --version
kafa doctor --repo .
```

`validate_structure.py` and source-repo doctor fail closed when the plugin inventory, schema files, Hooks, templates, runtime boundary, or package metadata drift from the approved contract.

`kafa doctor --repo .` is for the Kafa source repository. To inspect an ordinary project that uses Kafa, run:

```bash
kafa project doctor --repo /path/to/business-project
```

Project doctor checks initialized local state, runtime ignore rules, schema compatibility, and actionable next commands. It does not expect the Kafa source tree inside the business project.

## Repo-scoped installation

Repo scope writes `.agents/plugins/marketplace.json` and points Codex at the plugin under `plugins/codex-project-harness`:

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

Restart Codex, open the plugin directory, select the `kafa-local` marketplace, and install `codex-project-harness`.

## User-scoped installation

User scope copies the plugin to `~/.agents/plugins/codex-project-harness` and writes `~/.agents/plugins/marketplace.json`:

```bash
python3 -m pip install -e .
kafa plugin install --scope user --repo .
codex plugin add codex-project-harness@kafa-local
kafa doctor --scope user --repo .
```

The user-scope doctor verifies the marketplace entry, managed plugin copy, Codex registration, cache identity, and exact local plugin inventory. A copied directory or marketplace entry alone is not reported as a completed installation.

Use `--force` only when you intentionally want to replace an existing managed user copy:

```bash
kafa plugin install --scope user --repo . --force
```

## Expected plugin inventory

An isolated installation must expose exactly:

- seven Skills: `project-harness`, `minimal-safe-change`, `bug-fix-loop`, `test-first-delivery`, `independent-quality-gate`, `harness-audit`, and `project-retrospective`;
- three Hooks: `SessionStart`, `SubagentStart`, and `Stop`;
- three agent templates: `architect.toml`, `developer.toml`, and `qa-reviewer.toml`;
- the approved local runtime, core, schemas, and project templates.

Initialization copies only the three approved agent templates into `.codex/agents/` when they do not already exist. Existing files are not overwritten. Native Codex/ChatGPT remains the sole owner of task, subagent, worktree, approval, model, cancel, steer, and handoff behavior.

Hooks are advisory:

- `SessionStart` reads initialized local status.
- `SubagentStart` injects the root-controller single-writer boundary.
- `Stop` warns only.

When a project is not initialized, all three return a concise not-initialized result without creating `.ai-team` or raising a traceback.

## Initialize a business project

Use the public Kafa project entrypoint from the installed plugin or source checkout:

```bash
kafa project init --repo /path/to/business-project
kafa project doctor --repo /path/to/business-project
```

Initialization creates the active schema 31 local runtime at:

```text
.ai-team/state/harness.db
```

Schema 31 closes the public entity states: requirements and acceptance criteria
use only `active` or `cancelled`; failure modes use only `identified`,
`accepted`, or `exempt`. CLI guards, SQLite, migration preflight, doctor,
projections, and JSON schemas reject unknown or approximately matching values.
The only legacy normalization is schema-30 failure-mode `active` to
`identified`, which is counted in the migration report.

It also creates local Markdown views and the approved Native Codex agent templates. No remote credentials are requested, and no network call is part of project initialization.

## Canonical project path safety

Kafa-owned database, lock, sentinel, projection, template, and execution-artifact
paths use one pinned project filesystem authority. A root-level symlink alias is
resolved once when the operation starts. Every descendant must remain an ordinary
directory or a regular single-link file; Kafa rejects symlinks, junctions, reparse
points, hard-linked files, non-regular targets, and cross-device ancestors.

The stable diagnostic is:

```text
unsafe-project-path: <relative>: <reason>
```

The closed reason set is:

- `invalid-relative-path`
- `unsafe-ancestor`
- `unsafe-target`
- `hard-linked-target`
- `cross-device-ancestor`
- `path-identity-changed`
- `platform-safety-unavailable`

Treat the reported relative path as untrusted authority. Stop the operation, preserve
the database, sentinel, manifest, backups, and the reported object's bytes and metadata,
then establish who owns the object. If the path is intended to be Kafa state, restore an
ordinary in-project directory or regular single-link file from independently verified
bytes and rerun `kafa project doctor`. Kafa never automatically follows, rewrites,
deletes, or repairs an unsafe link, and operators must not remove a linked sentinel or
replace a linked database merely to make doctor green.

Python's standard `sqlite3` API cannot open an already pinned file descriptor. Kafa
therefore precreates or validates the DB family, connects with a no-create URI, and
rechecks filesystem identity immediately after connect, after journal setup, and before
close. A bounded identity exchange is closed and rejected; a continuously active
attacker with the same user authority is outside this guarantee. Inspect such a project
under an isolated OS user or container before retrying.

Project path safety does not sandbox arbitrary verification commands. Use the explicit
container runner when a target requires sandbox or no-network execution; the path seam
protects Kafa's own artifact reads and publications, not arbitrary command behavior.
If an unsafe path prevents DB or projection restoration, retain every recovery artifact
and follow the `rollback-incomplete` procedure below. Never relabel an incomplete restore
as successful and never auto-repair the link.

## Schema 31 execution provenance

Local verification records `target_definition_sha256`, controller `platform`,
`runtime_executable`, `runtime_version`, `runtime_executable_sha256`,
`policy_version`, and `provenance_status=complete` before the result can become
delivery evidence. Doctor checks complete rows against the same contract. Executions
migrated from schema 30 or older are retained as `legacy-incomplete` history and cannot
satisfy a new schema 31 delivery.

Container verification requires an already-local Docker image or Linux native-local
Podman image. Kafa freezes and records `container_engine_endpoint`, accepts only a local
Unix socket or Windows named pipe for Docker, pins every daemon command to it, resolves
`container_image_digest`, invokes the immutable identity with `--pull=never`, and
rechecks endpoint/engine/image identity before commit. Remote or ambiguous Docker
routing and Podman remote/machine routing fail closed. Kafa does not pull images. A
missing image, provenance drift, or incomplete provenance creates no passing validation;
prepare or update the local image explicitly outside Kafa, then rerun verification.

## Upgrade

First pull or checkout the desired Kafa source version, then refresh the installation:

```bash
git pull
python3 -m pip install -e .
kafa plugin upgrade --repo .
```

For user scope:

```bash
kafa plugin upgrade --scope user --repo .
```

Restart Codex after upgrading so it reloads plugin metadata and Hooks.

## Migrate an initialized project to schema 31

Always inspect the current project first:

```bash
kafa project status --repo /path/to/business-project
```

For a schema 27, 28, 29, or 30 project, verify the CLI contract and run the dry-run before the real migration. Pass the actual source version reported by `status`; the following example uses schema 30:

```bash
kafa project migrate --repo /path/to/business-project --help

kafa project migrate --repo /path/to/business-project \
  --from-version 30 \
  --to-version 31 \
  --dry-run

kafa project migrate --repo /path/to/business-project \
  --from-version 30 \
  --to-version 31
```

Schema 27, 28, and 29 projects use the supported legacy conversion path before activation of schema 31. Schema 30 is a fixed compatibility and migration source, not an active runtime target. Never guess `--from-version`.

The migration is side-by-side and recoverable:

1. Validate the source database and compare-and-swap its schema version.
2. Create a consistent SQLite backup with SHA-256, integrity result, foreign-key result, and row counts.
3. Convert only approved local delivery facts into a staging schema 31 database.
4. Validate schema inventory, foreign keys, invariants, and projection dry-run.
5. Atomically activate the staging database.
6. Run final doctor; if it fails after activation, restore the verified backup and preserve the failed database for diagnosis.

Before atomic replacement, migration atomically persists and fsyncs a
`recovery-required` sentinel with the manifest path. The sentinel is removed
only after successful migration or a verified complete rollback of both the DB
and every generated projection. A `rollback-incomplete`, hard process exit, or
interrupted recovery keeps the sentinel fail-closed; operators must not remove
it until the manifest has been used to recover and verify database/projection
authority. A core caller without the mandatory projection activation validator
is rejected and cannot report schema 31 activated.

The validator proves content, not only path presence: it renders all 13 views
from an independent temporary database snapshot and compares the live bytes.
All production projection publication holds the project operation lock from its
first database read through its final file write. `project-state.yaml` derives
its timestamp from SQLite `project.updated_at`, not the render-time clock, and
rebuild uses replace rather than merge semantics so unchanged facts are
byte-stable and stale ad-hoc keys are removed. The exact keys include database
`id` and `current_cycle_id` and exclude generic `blocked_reason`. During rollback, failed
failed-schema WAL/SHM files are quarantined with the failed main database before the
source backup is restored and opened through ordinary read-only SQLite
semantics. A handle or sidecar that cannot be neutralized leaves
`rollback-incomplete`; it is never masked by an immutable SQLite open.
Core repeats independent projection content verification after the publication
callback; callback self-report cannot activate migration. Operation-lock open
and unlock cleanup is `BaseException`-safe and preserves the cancellation after
releasing local and OS resources.
Core also fingerprints the stabilized active DB before and after the callback;
any callback database write rolls back even when its value and regenerated
views would otherwise pass doctor.

Migration artifacts are stored under `.ai-team/backups/`. Removed remote-collaboration and execution-lifecycle rows remain only in that backup and never enter the active schema 31 database.

Git source inspection pins the requested root with controlled `GIT_WORK_TREE`.
Schema 31 permits exactly 30 active tables plus SQLite's `sqlite_sequence`;
other reserved-prefix tables are corruption, not hidden internals. Real Native
controller verification runs from a start-verified private Git-backed snapshot
and compares the original source again at completion. Snapshot initialization,
hashing, and index construction ignore ambient `GIT_DIR`/global config and use
an explicit empty template.

After migration:

```bash
kafa project doctor --repo /path/to/business-project
kafa project validate --repo /path/to/business-project
kafa project status --repo /path/to/business-project
```

Do not delete the verified backup until the migrated project has passed its required local checks. After new schema 31 facts are written, Kafa does not promise an automatic downgrade.

## Repair and local recovery

Inspect the planned repair first:

```bash
kafa project repair --repo /path/to/business-project --dry-run
```

Any mutating repair creates and verifies a SQLite backup before changing the active database. `projection rebuild` regenerates supported Markdown views from SQLite and does not make those views a recovery source:

```bash
kafa project projection --repo /path/to/business-project rebuild
```

Do not manually replace `harness.db` unless a diagnosed failure requires operator recovery and the selected backup digest and integrity results have been independently checked.

If Store or `kafa project doctor` reports `recovery-required` or
`rollback-incomplete`, preserve the sentinel, active DB, manifest, failed DB,
and projection backup together. Complete and verify recovery first. Only an
ordinary pre-activation stale sentinel may be considered for removal after the
owner is confirmed inactive and database/projection authority has been checked;
operators must not remove a recovery-required sentinel as stale.

The project `status`, `doctor`, `validate`, and `quickstart status` paths check
this sentinel before deciding that a missing `harness.db` means uninitialized.
They report the manifest and do-not-remove guidance and do not recommend `init`
as a recovery action. A handled pre-activation failure clears its diagnostic
sentinel only after the source database is verified unchanged and the bounded
projection backup has been restored and verified; an unverified backup failure
keeps the diagnostic sentinel.

## Isolated installation verification

The isolated smoke test proves plugin discovery and exact inventory in a temporary HOME:

```bash
python3 tests/run_isolated_install_smoke.py --repo .
```

A real Native Codex compatibility profile is selected by the closed release
change-scope classifier. Host, packaging, release-tooling, Native-evaluator,
and unknown paths require blocking single and parallel profiles; advisory
scopes still run every deterministic gate. CI keeps volatile detail as a
retained artifact and exposes a digest-bound stable summary. The summary never
replaces its detail. If selected evidence is unavailable, not run, blocked, or
fails, report that state exactly; isolated discovery or fixture results do not
replace it.

## Uninstall

Remove only the repo-scoped marketplace entry:

```bash
kafa plugin uninstall --repo .
```

For user scope, remove the marketplace entry and managed copied plugin directory:

```bash
kafa plugin uninstall --scope user --repo . --remove-files
```

Uninstall does not delete Codex caches, business-project `.ai-team/` state, migration backups, or generated project views.

## Migration from a manual plugin path

If Codex previously pointed at this plugin manually:

1. Keep the complete `plugins/codex-project-harness` directory.
2. Run `python3 -m pip install -e .`.
3. Run `kafa plugin install --repo .`.
4. Restart Codex and install from the `kafa-local` marketplace.
5. Remove the old hand-written marketplace entry only after the managed entry is discoverable.

## macOS, Linux, and Windows

- macOS/Linux examples use `python3`; Windows can use `py -3.11`.
- If Python reports `externally-managed-environment`, create a virtual environment: `python3 -m venv .venv`, activate it, then run `python -m pip install -e .`.
- Quote paths that contain spaces when passing `--repo` or `--plugin-path`.
- Repo-scope installation writes inside the Kafa checkout. User scope writes under the current user's home directory.
- Business-project runtime state and backups must remain ignored by Git unless the project has an explicit, reviewed policy to do otherwise.

在项目根创建文档建议的 `.venv/` 不会使 candidate identity 因解释器符号链接失效。
Kafa 仅排除精确、未版本化的 top-level dependency/tool environment：`.venv/`、
`venv/`、`.tox/`、`.nox/` 和 `node_modules/`，以及生成工具缓存；普通 ignored
runtime source 仍会被哈希，`.venvish/` 不会被模糊排除，项目 lockfile 与依赖
manifest 仍会改变 candidate identity。

除 reserved `.ai-team/` 状态外，Kafa 只排除 exact generated projection、retired
projection 和三个静态 agent template。`.gitignore` 以及额外的
`.codex/agents/`、`docs/harness/` runtime 文件仍会改变 candidate；no-Git 项目中的
FIFO、socket、device 或其他非普通路径会 fail closed。Git index 和 HEAD 都会独立
检查非普通 mode；HEAD-only gitlink 即使删除已暂存也会使 identity fail closed。
Identity commands also disable Git replace-object lookup, so `refs/replace`
cannot substitute a clean commit/tree/blob for the original local authority.

Structured result 文件若由 verification command 生成，应使用 `.ai-team/runtime/`
下的项目相对路径或 stdout。普通项目路径中的新结果文件属于 candidate source，生成后
会触发 stale-candidate 保护，而不会被动态加入排除清单。

## Troubleshooting

- `kafa: command not found`: reinstall with `python3 -m pip install -e .`, then reopen the terminal.
- `plugin manifest not found`: run from the Kafa repository root or pass `--repo /path/to/kafa`.
- `target plugin already exists`: use `kafa plugin upgrade --scope user --repo .` or intentionally pass `--force`.
- Plugin does not appear in Codex: restart Codex, confirm the marketplace file exists, and run the matching doctor command.
- Hook reports not initialized: run `init` in the intended business-project root; the Hook itself will not create state.
- Schema mismatch: run `status`, verify the current schema, inspect `migrate --help`, and use `--dry-run` before any migration.
- Repair is blocked: preserve the active database and backup artifacts, then investigate the reported invariant or integrity failure instead of bypassing it.

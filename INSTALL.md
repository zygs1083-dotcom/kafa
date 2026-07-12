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

Run these commands from the Kafa repository root:

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

Use the consolidated `project-harness` proxy from the installed plugin or source checkout:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project init

kafa project doctor --repo /path/to/business-project
```

Initialization creates the schema 30 local runtime at:

```text
.ai-team/state/harness.db
```

It also creates local Markdown views and the approved Native Codex agent templates. No remote credentials are requested, and no network call is part of project initialization.

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

## Migrate an initialized project to schema 30

Always inspect the current project first:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project status
```

For a schema 29 project, verify the CLI contract and run the dry-run before the real migration:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project migrate --help

python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project migrate \
  --from-version 29 \
  --to-version 30 \
  --dry-run

python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project migrate \
  --from-version 29 \
  --to-version 30
```

Published schema 27 and development schema 28 projects use the isolated legacy conversion stage before the same schema 30 conversion. Pass the actual source version reported by the project; never guess `--from-version`.

The migration is side-by-side and recoverable:

1. Validate the source database and compare-and-swap its schema version.
2. Create a consistent SQLite backup with SHA-256, integrity result, foreign-key result, and row counts.
3. Convert only approved local delivery facts into a staging schema 30 database.
4. Validate schema inventory, foreign keys, invariants, and projection dry-run.
5. Atomically activate the staging database.
6. Run final doctor; if it fails after activation, restore the verified backup and preserve the failed database for diagnosis.

Migration artifacts are stored under `.ai-team/backups/`. Removed remote-collaboration and execution-lifecycle rows remain only in that backup and never enter the active schema 30 database.

After migration:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project doctor
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project validate
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project status
```

Do not delete the verified backup until the migrated project has passed its required local checks. After new schema 30 facts are written, Kafa does not promise an automatic downgrade.

## Repair and local recovery

Inspect the planned repair first:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project repair --dry-run
```

Any mutating repair creates and verifies a SQLite backup before changing the active database. `projection rebuild` regenerates supported Markdown views from SQLite and does not make those views a recovery source:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root /path/to/business-project projection rebuild
```

Do not manually replace `harness.db` unless a diagnosed failure requires operator recovery and the selected backup digest and integrity results have been independently checked.

## Isolated installation verification

The isolated smoke test proves plugin discovery and exact inventory in a temporary HOME:

```bash
python3 tests/run_isolated_install_smoke.py --repo .
```

A real Native Codex compatibility profile is a separate, explicit opt-in check. If it is unavailable, not run, blocked, or fails a scenario, report that state exactly; isolated discovery or fixture results do not replace it.

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
- Quote paths that contain spaces when passing `--repo`, `--plugin-path`, or `--root`.
- Repo-scope installation writes inside the Kafa checkout. User scope writes under the current user's home directory.
- Business-project runtime state and backups must remain ignored by Git unless the project has an explicit, reviewed policy to do otherwise.

## Troubleshooting

- `kafa: command not found`: reinstall with `python3 -m pip install -e .`, then reopen the terminal.
- `plugin manifest not found`: run from the Kafa repository root or pass `--repo /path/to/kafa`.
- `target plugin already exists`: use `kafa plugin upgrade --scope user --repo .` or intentionally pass `--force`.
- Plugin does not appear in Codex: restart Codex, confirm the marketplace file exists, and run the matching doctor command.
- Hook reports not initialized: run `init` in the intended business-project root; the Hook itself will not create state.
- Schema mismatch: run `status`, verify the current schema, inspect `migrate --help`, and use `--dry-run` before any migration.
- Repair is blocked: preserve the active database and backup artifacts, then investigate the reported invariant or integrity failure instead of bypassing it.

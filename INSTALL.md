# Installation

Codex Project Harness is distributed as a local Git/Codex plugin bundle. Phase 6 adds the `kafa` helper CLI for repeatable local marketplace setup; it does not publish to PyPI and does not mutate Codex plugin caches directly.

## Requirements

- Python 3.11 or newer.
- Git on `PATH`.
- Codex with plugin marketplace support.
- A checkout of this repository.

Check the repository:

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 -m pip install -e .
kafa --version
kafa doctor --repo .
```

Expected:

```text
OK: plugin structure is valid
1.16.0-beta.1
```

`kafa doctor --repo .` also checks the architecture control plane contract: Skill Entry, Plugin Distribution, Hooks Advisory Layer, Host Bridge/Provider Layer, Kernel Trust Layer, and Connector/Eval Boundary must still declare their non-bypass responsibilities.

## Install For This Repo

Repo scope is the default. It writes `.agents/plugins/marketplace.json` and points Codex at the plugin already stored under `plugins/codex-project-harness`.

```bash
python3 -m pip install -e .
kafa plugin install --repo .
```

Restart Codex, open the plugin directory, choose the `kafa-local` marketplace, and install `codex-project-harness`.

## Install For Your User Account

User scope copies the plugin to `~/.agents/plugins/codex-project-harness` and writes `~/.agents/plugins/marketplace.json`.

```bash
python3 -m pip install -e .
kafa plugin install --scope user --repo .
```

Use `--force` only when you intentionally want to replace an existing copied user plugin:

```bash
kafa plugin install --scope user --repo . --force
```

## Upgrade

Pull or checkout the desired repository version first, then refresh the marketplace entry.

```bash
git pull
python3 -m pip install -e .
kafa plugin upgrade --repo .
```

For user scope:

```bash
kafa plugin upgrade --scope user --repo .
```

Restart Codex after upgrading so it reloads plugin metadata and hooks.

## Uninstall

Remove only the marketplace entry:

```bash
kafa plugin uninstall --repo .
```

For user scope, remove the marketplace entry and the managed copied plugin directory:

```bash
kafa plugin uninstall --scope user --repo . --remove-files
```

Uninstall does not delete Codex caches or project `.ai-team/` state.

## Migration From Manual Install

If you previously pointed Codex at the plugin directory manually:

1. Keep the full `plugins/codex-project-harness` directory in the repository.
2. Run `python3 -m pip install -e .`.
3. Run `kafa plugin install --repo .`.
4. Restart Codex and install from the `kafa-local` marketplace.
5. Remove any old hand-written marketplace entry only after the new entry appears.

## macOS, Linux, And Windows Notes

- macOS/Linux examples use `python3`; Windows can use `py -3.11 -m pip install -e .` and then `kafa ...`.
- If Python reports `externally-managed-environment` (common with Homebrew Python), create a virtual environment first: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -e .`.
- Paths with spaces are supported when passed as quoted `--repo` or `--plugin-path` values.
- Repo-scope install writes inside the current repository. User-scope install writes under the current user's home directory.
- No secrets or connector tokens are required for installation.

## Troubleshooting

- `kafa: command not found`: run `python3 -m pip install -e .` again, then reopen the terminal.
- `externally-managed-environment`: use a virtual environment or pipx-style app environment rather than forcing a system install.
- `plugin manifest not found`: run commands from the repository root or pass `--repo /path/to/kafa`.
- `target plugin already exists`: use `kafa plugin upgrade --scope user --repo .` or pass `--force`.
- Plugin does not appear in Codex: restart Codex and confirm `.agents/plugins/marketplace.json` exists.
- Hooks do not run: review and trust plugin hooks with `/hooks`; set `CODEX_PROJECT_HARNESS_PLUGIN_ROOT` if the plugin is outside the default repo path.
- `control plane contract` fails in `kafa doctor`: inspect the named layer and restore the boundary text or implementation path before release.

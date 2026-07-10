# Wave 3 Installation and Release Recovery Evidence

Date: 2026-07-10

Branch: `v1.26-stop-ship-correctness`

Status: implementation complete locally; remote macOS/Linux/Windows workflow evidence remains pending until an authorized push.

## Closed Findings

| Finding | Commit | Closure evidence |
| --- | --- | --- |
| IN-001 user marketplace source | `a23d988` | User marketplace source resolves to the managed user plugin directory. |
| IN-002 ordinary-project launcher | `9cd0e74` | `kafa project init/status/quickstart` locate installed runtime without vendored plugin source. |
| IN-003 installed hooks | `85dbb6a` | Hook commands use Codex `PLUGIN_ROOT`, include Windows overrides, read manifest version, resolve session cwd, and reject legacy root override. |
| IN-004 doctor and install validation | `63cd7f3` | Static doctor validates source, marketplace, managed copy, Codex registration/cache, hook definition, and content identity without executing untrusted checkout code. |
| PK-001 mandatory Host SDK | `43e58c7` | Base wheel is stdlib-only; `openai-codex>=0.1.0b3` is available only through `kafa[host-codex]`; missing SDK fails closed. |
| RL-001 release fact split | `8d2e80e` | `release.json` governs development/release state, versions, tag, notes, runtime/schema, Codex smoke version, artifact build, checksums, and tag-gated prerelease workflow. |

## Deterministic Evidence

- Full local regression after RL-001: `299 tests`, `OK`, 488.048 seconds.
- Full local regression after PK-001: `291 tests`, `OK`, 465.098 seconds.
- Full local regression after IN-004: `290 tests`, `OK`, 475.988 seconds.
- Plugin structure validation: `OK: plugin structure is valid`.
- Development release contract: all checks pass with `release_state=development`, schema `29`, runtime/kernel `4.18.0`, and no matching tag.
- `--require-tag` rejects development state, a missing/mismatched tag, stale runtime/schema notes, and a dirty worktree.
- A synthetic clean release commit with matching manifest, dated notes, and tag passes `--require-tag`.
- Real `codex-cli 0.143.0` isolated smoke passes marketplace discovery, plugin add/list, cache identity, cached hook execution, doctor, and remove.
- Final-artifact mode passes using the exact wheel and full source archive that a release workflow would upload.
- Real `kafa[host-codex]` installation resolves the SDK and platform CLI wheel in an isolated venv; base wheel metadata has no unconditional SDK dependency.
- `git diff --check` and workflow YAML parsing pass.

## Security Boundaries

- `kafa doctor` is static-only. It does not execute checkout validation scripts or installed hook Python.
- Runtime hook execution occurs only in the explicit isolated CI/release smoke after Codex installs the plugin into its cache.
- Managed plugin and cache trees reject symlinks, Windows junctions, and other reparse points.
- Release publication waits for Ubuntu, macOS, and Windows verification jobs through `needs: verify`.
- The release workflow builds final artifacts before smoke, verifies those exact artifacts, writes `SHA256SUMS`, and only then calls `gh release create --verify-tag --prerelease`.
- Current source is not released: `release.json` is `development`, Changelog says `Unreleased`, and no v1.25 tag is present.

## Official Contract Used

- Codex hook commands run with session cwd and receive `PLUGIN_ROOT` / `PLUGIN_DATA`: <https://developers.openai.com/codex/hooks>
- Plugin component paths and hook roots are relative to the installed plugin root: <https://developers.openai.com/codex/plugins/build>

## Pending External Evidence

- GitHub-hosted macOS/Linux/Windows jobs have not run for these commits because the branch has not been pushed.
- No tag, GitHub Release, PyPI upload, merge, or deployment was performed.
- The repository remains stop-ship; Wave 4 through Wave 7 and final compatibility gates are still required before changing `release_state` to `release`.

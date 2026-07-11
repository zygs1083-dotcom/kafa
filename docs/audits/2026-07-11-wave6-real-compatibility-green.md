# Wave 6 Real Compatibility and Release Gate Evidence

Date: 2026-07-11

Branch: `v1.26-stop-ship-correctness`

Status: Wave 6 is green on the assembled local branch. This document does not
authorize a push, merge, tag, release, or deployment.

## What Wave 6 Proves

Wave 6 separates three kinds of evidence that were previously easy to
conflate:

1. deterministic Kernel tests prove local decision and failure semantics;
2. isolated installation smoke proves the tagged plugin can be installed and
   discovered by a real Codex app-server;
3. the live profile proves one authenticated Codex host can execute the real
   plugin, hook, thread, turn, git-worktree, native receipt, controller verify,
   and integration path.

Fixture, monkeypatch, fake SDK, direct hook execution, or a skipped live profile
is not counted as real-host compatibility evidence.

## Findings and Commits

| Finding or compatibility gap | Commits | Evidence |
| --- | --- | --- |
| EV-001 live profile permanently skipped | `96abb89`, `644c663`, `4fa890b` | An explicit live request now returns `passed`, `failed`, or `blocked`; a disabled profile returns non-success `not-run`. The implemented profile launches authenticated Codex CLI 0.143.0 and app-server. |
| EV-002 fixture bypassed delivery validation | `96abb89`, `644c663`, `b36129a` | Fixture success uses the public integration path and validations are bound to the candidate that actually executed. No release-critical `validate_runtime` monkeypatch remains. |
| IN-004 install checks did not prove host discovery | `eedf373` | Isolated smoke installs the current marketplace into a temporary home, launches real app-server, and checks exact plugin ID/version, 12 Skills, and five Hooks. |
| Stop Hook was rejected by the real host | `cdb3919` | Successful Stop output is valid JSON with `continue`, `systemMessage`, and `suppressOutput`; strict failure remains exit 2 on stderr. |
| Native host-managed worktree failed schema validation | `ffba32a` | `dispatch-worktree.schema.json` accepts the audit state `host-managed`, and native import is followed by a successful Kernel doctor check. |
| Release publication did not depend on live compatibility | `9d7c594` | `publish` now depends on both the deterministic `verify` matrix and a mandatory authenticated `real_host_compatibility` job. |
| AP-001 lacks a host-verifiable Apps/MCP receipt | `c5cfe50` | Accepted as a bounded `legacy-direct` risk, not represented as implemented. Public Codex 0.143.0 schemas expose tool-call correlation and results but no result-bound signature/verifier. |

## Real Isolated Installation

`tests/run_isolated_install_smoke.py` now uses a temporary `HOME` and
`CODEX_HOME`, installs the current source through the public Kafa installer,
adds the local marketplace plugin through Codex, and launches a real
`codex app-server --stdio` process.

The current smoke verifies:

- plugin ID `codex-project-harness@kafa-local`;
- local plugin version `1.25.0-beta.1`;
- all 12 repository Kafa Skills are present in host discovery;
- Hook discovery contains exactly `sessionStart`, `subagentStart`,
  `preToolUse`, `postToolUse`, and `stop` for the plugin;
- the cache paths point at the isolated installed plugin, not the source tree;
- direct handler execution and host execution are reported separately.

Discovery alone does not claim that a Hook executed. Only real
`hook/started`/`hook/completed` notifications in the live turn count as host
execution evidence.

## Real Authenticated Codex Profile

Command:

```bash
HARNESS_E2E_ENABLE_LIVE_CODEX=1 \
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py \
  --mode live-codex \
  --out /tmp/kafa-live-codex-report-current.json
```

Observed host:

```text
Codex CLI: codex-cli 0.143.0
platform: macOS 15.7.7 arm64
Python: 3.14.5
Git: 2.54.0
live_status: passed
scenario_count: 2
passed_count: 2
failed_count: 0
skipped_count: 0
duration_seconds: 49.170779
```

The real app-server scenario observed:

- the installed plugin and all 12 Skills;
- all five Kafa Hooks in discovery;
- host execution of `SessionStart`, `PreToolUse`, `PostToolUse`, and `Stop`;
- a real ephemeral thread and completed turn;
- `workspaceWrite` sandbox with network disabled and approval policy `never`;
- a real edit to `app.py` in an isolated git worktree.

The real native-receipt scenario then proved:

```text
receipt provenance: audit-only
evidence before import: 0
evidence after import: 0
evidence after controller verify: 1
integration return code: 0
run status: integrated
```

The imported host receipt therefore remains a raw report/attempt. It cannot
self-promote into evidence, and public `dispatch verify-attempt` plus
integration remain mandatory.

## Real-Run Defects Found During the Eval

The live run found defects that the fake SDK path did not expose:

1. stripping the whole porcelain output removed the first status-space and
   caused `app.py` to be parsed as `pp.py`;
2. writing the native receipt in the worktree root made the candidate dirty;
3. file-claim setup used an empty assignment `agent_id` while the native
   package correctly fell back to the `developer` capability;
4. a successful Stop Hook printed plain text even though Codex 0.143.0 requires
   JSON on exit 0;
5. native import persisted `host-managed`, which the checked-in worktree schema
   did not permit.

Each defect was reproduced with a regression test before the live profile was
accepted as passing.

## Apps/MCP Receipt Boundary

Wave 6 generated the public Codex 0.143.0 app-server schemas and inspected the
MCP result surface. It found:

- `McpServerToolCallResponse` returns content, structured content, `_meta`, and
  error status;
- `McpToolCallThreadItem` exposes item/server/tool/arguments/status/result/error
  correlation plus App context;
- progress notifications bind item, thread, and turn IDs;
- approval and elicitation lifecycle is observable;
- no MCP result contains an issuer, signature, verification key, signed digest,
  or binding to the Kafa action, fence, project, scope, idempotency key, and
  request hash.

The separate `attestation/generate` request obtains an opaque client token for
upstream `x-oai-attestation`; the public schema does not attach it to an MCP
tool result. It is not a connector result receipt.

The detailed decision and current accepted risk are recorded in
[`APPS_MCP_RECEIPT_ADR.md`](../runtime/APPS_MCP_RECEIPT_ADR.md). No fake
receipt adapter or model-authored signature was added.

## Release Dependency Graph

The tag-only release workflow now has two independent prerequisites:

```text
verify (Linux, macOS, Windows deterministic matrix) --+
                                                     +--> publish
real_host_compatibility (authenticated self-hosted) -+
```

The live job is not optional and does not use `continue-on-error`. It requires
a protected `codex-live-release` environment and a self-hosted runner labelled
`kafa-codex-live`, installs the Codex version pinned in `release.json`, runs the
live profile with an explicit enable flag, and uploads the JSON report even on
failure. A missing runner, missing authentication, blocked capability,
not-run profile, or failed scenario prevents publication.

## Verification

Targeted tests on the current branch:

```text
python3 -m unittest tests/test_agent_e2e_eval.py
Ran 13 tests - OK

python3 -m unittest tests/test_codex_hooks.py
Ran 13 tests - OK

python3 -m unittest tests/test_native_codex_receipts.py
Ran 7 tests - OK

python3 -m unittest tests/test_install_release.py tests/test_release_contract.py
Ran 33 tests - OK

python3 -m unittest tests/test_control_plane_architecture.py tests/test_release_contract.py
Ran 16 tests - OK
```

Complete Wave 6 matrix on the assembled branch:

```text
py_compile: passed
validate_structure.py: passed
release workflow YAML parse: passed
unittest discover: 349 tests in 572.149s, OK
runtime smoke: 3 scenarios passed
forward eval: passed (runtime smoke wrapper, 3 scenarios)
skill eval: 15 markers passed
fixture E2E: 5/5 passed
stability E2E: 12/12 passed
  failed_count=0
  false_pass_count=0
  forged_evidence_block_count=1
  sqlite_lock_error_count=0
  human_intervention_count=0
isolated install smoke: passed with codex-cli 0.143.0
real live-codex: 2/2 passed, live_status=passed, skipped=0
kafa release contract: ok=true, release_state=development
kafa doctor after repo-scope install: ok=true
git diff --check: passed
```

## Explicit Non-Claims and Residual Risk

- The live turn did not request or observe a native child subagent. The report
  records `native_subagent_observed=false`.
- The git worktree was created and owned by the live eval runner, not claimed
  as a host-created native worktree receipt.
- No live GitHub, Linear, Notion, Figma, or Slack write was issued.
- Apps/MCP host-attested connector receipt coverage is unavailable on the
  pinned public contract and remains an accepted `legacy-direct` risk.
- The self-hosted release runner and protected environment must be configured
  in GitHub before any tag can publish.
- The repository remains in `development` release state.
- The delegated independent Apps/MCP review hit the account usage limit. Its
  incomplete run is not counted as review evidence; the main-agent result is
  grounded in generated public schemas and the real app-server surface.

## Exit Decision

Wave 6 exits green because the complete unit suite, deterministic runtime/eval
matrix, isolated installation, authenticated real-host profile, release
contract, root doctor, and whitespace check all pass on the assembled branch.
The release dependency graph now requires both deterministic and real-host
evidence. Wave 7 architecture deepening may begin, but the repository remains
stop-ship and `release_state=development`; no publication is authorized.

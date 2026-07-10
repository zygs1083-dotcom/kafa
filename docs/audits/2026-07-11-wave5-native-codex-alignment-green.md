# Wave 5 Native Codex and ChatGPT Alignment Evidence

Date: 2026-07-11

Branch: `v1.26-stop-ship-correctness`

Status: Wave 5 architecture and local correctness scope is green. Real host
compatibility remains a Wave 6 release gate. This document does not authorize a
push, merge, tag, release, or claim that fake SDK tests prove live Codex
compatibility.

## Architecture Decision

Commit `af44971` records the accepted native runtime ownership contract in
[`docs/runtime/NATIVE_CODEX_RUNTIME_ADR.md`](../runtime/NATIVE_CODEX_RUNTIME_ADR.md).

Native Codex or ChatGPT owns task, thread, subagent, managed worktree,
approval, sandbox, model, cancel, steer, handoff, fork, and archive lifecycle.
Kafa owns immutable delivery constraints, root-workspace facts, receipt
validation, controller verification, integration, and delivery decisions.
Kafa assignment state is not presented as an authoritative mirror of the host
lifecycle.

## Closed Findings

| Finding | Commits | Closure evidence |
| --- | --- | --- |
| HP-001 legacy Host lifecycle | `4423a6a` | The optional legacy worker has an independent deadline watchdog, worker liveness checks, terminal report CAS, stable process-tree termination, dead-worker lease protection, and transaction-time session/fence/status rechecks. Timeout, cancellation, dead worker, or unconfirmed termination cannot create a report, attempt, or evidence and leaves the assignment/run `verification_failed`. |
| HP-002 parent permission mismatch | `45830f0`, `4423a6a` | Native host policy is authoritative and is reported in the native receipt. The legacy SDK path is disabled unless the operator explicitly sets `HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all`; `spawn`, the worker entrypoint, and the SDK execution boundary all enforce the policy. Legacy execution never claims to inherit the parent task policy. |
| HP-003 hidden standalone subagent | `af44971`, `f1c575d` | `dispatch native-export/native-import` exchange immutable task packages and host receipts containing real host task/thread/worktree IDs. Kafa does not manufacture native IDs or own native resume, steer, cancel, handoff, or archive state. Imported receipts create only raw reports and attempts. |
| ST-001 worktree/cloud state split | `af44971`, `f1c575d` | Mutable SQLite facts remain single-writer in the project root. Managed worktrees receive immutable packages, never copied databases. Hosted Kernel mutation is unsupported until an authenticated Project Fact Transport exists; root import and verification remain mandatory. |
| PK-001 mandatory Host SDK | `43e58c7` | Base packaging has no dependencies. `openai-codex>=0.1.0b3` is available only through the optional `kafa[host-codex]` extra for the legacy adapter. |
| HP-004 fake `manual-csv` provider | `5459ad8` | `manual-csv` was removed from the provider registry and CLI choices. CSV remains an explicit controller-mediated export/import exchange format, not a running provider lifecycle. |
| MR-001 hard-coded native model routing | `4caf435` | Native packages contain capability and risk hints, not concrete model slugs. Route advice is host-neutral and read-only. Native model/reasoning selection belongs to the host receipt; the old Spark policy is legacy-only and requires an explicit model value. |

## Native Receipt Trust Boundary

Commit `f1c575d` adds the package and receipt exchange with these fail-closed
properties:

- packages bind run, assignment, cycle, candidate, branch, role, acceptance,
  failure modes, targets, file claims, and capability hints with a SHA-256 hash;
- packages contain no SQLite database, runtime directory, session secret,
  HMAC key, connector token, or concrete model slug;
- imports reject placeholder host identities, package hash mismatch, changed
  task constraints, branch/base/head mismatch, and missing required policy;
- identical receipt import is exactly once, while a conflicting receipt is
  rejected;
- receipt import records real host identifiers and one raw report/attempt but
  creates no delivery-eligible evidence;
- controller verification and the existing integration/delivery gates remain
  the only path to trusted delivery evidence.

## Legacy Host Fail-Closed Boundary

The legacy `host-codex` adapter remains available only for explicit local
compatibility use. It is not a native subagent implementation and is not
selected by native route advice.

The local worker and watchdog can terminate the process group and descendants
that remain discoverable. They cannot prove that an SDK helper reparented
outside the known process tree has disappeared. Therefore cancellation and
timeout never produce a trusted cancelled-success state: the session,
assignment, and run remain `verification_failed`, late reports are ignored,
the assignment is not automatically replanned, and no evidence is generated.
Only the native host can provide authoritative task cancellation.

Direct modification of a legacy job file by the same local OS user remains a
legacy local threat surface. The job and environment must both opt in to the
restricted policy and the SDK boundary checks again, but this is not a signed
host transport. It is one reason the adapter remains optional, legacy, and
non-evidentiary instead of being used as the native control plane.

## Verification

Wave 5 targeted matrix on current HEAD:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests/test_native_codex_receipts.py \
  tests/test_host_codex_provider.py \
  tests/test_dispatch_route_advice.py \
  tests/test_codex_fanout_export.py \
  tests/test_install_release.py \
  tests/test_feature_freeze.py

Ran 72 tests in 92.604s
OK
```

Latest complete local regression after the legacy lifecycle hardening:

```text
Ran 339 tests in 527.451s
OK
```

Latest deterministic runners:

```text
runtime smoke: 3 scenarios passed
forward eval: passed
skill eval: 15 markers passed
fixture E2E: 5/5 passed
stability E2E: 12/12 passed
failed_count=0
false_pass_count=0
forged_evidence_block_count=1
sqlite_lock_error_count=0
human_intervention_count=0
kafa doctor --repo . --json: ok=true
git diff --check: passed
```

The host-provider scenarios use an injected fake `openai_codex` package. They
verify Kafa's deterministic boundary and failure handling only. They are not
evidence that a real Codex task, native subagent, approval, worktree, cancel,
steer, handoff, hook, or skill-discovery path works on the current host.

## Wave 5 Exit Decision

Wave 5 exits green because lifecycle authority has moved to native host facts,
Kafa exchanges immutable constraints and receipts without copying its
database, the optional legacy paths fail closed, CSV no longer pretends to be
a provider, and native model choice is host-owned.

Release remains blocked. Wave 6 must still replace misleading skipped/live and
monkeypatched fixture semantics, exercise real plugin installation and host
discovery, add an Apps/MCP receipt compatibility contract, and require at
least one real host compatibility profile before release authorization.

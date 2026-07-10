# ADR: Native Codex Runtime Ownership

- Status: Accepted direction; receipt runtime implementation follows
- Date: 2026-07-10
- Findings: HP-001, HP-002, HP-003, ST-001, HP-004, MR-001

## Context

Kafa currently models a `host-codex` provider that launches a standalone Codex
SDK worker, creates its own worktree, chooses sandbox/approval settings, polls a
JSON report, and approximates cancel/heartbeat/status. That is a second hidden
agent lifecycle beside the user-visible Codex/ChatGPT task and thread system.

The host already owns task/thread identity, native subagents, managed
worktrees, approval, sandbox, model policy, steering, interruption, handoff,
and archive state. Kafa cannot safely reproduce that lifecycle from a plugin
runtime script, and a standalone SDK thread does not become a user-visible
native subagent merely because it uses Codex.

Official references for the host capability direction:

- [Codex subagents](https://developers.openai.com/codex/concepts/subagents)
- [Codex git worktrees](https://developers.openai.com/codex/environments/git-worktrees)
- [Codex SDK](https://developers.openai.com/codex/codex-sdk)
- [Codex App Server](https://developers.openai.com/codex/app-server)

This ADR defines Kafa contracts. It does not claim that every Codex/ChatGPT
surface exposes one identical receipt API.

## Decision

### Lifecycle ownership

Native Codex/ChatGPT owns:

- task and thread creation;
- native subagent creation and role configuration;
- managed worktree creation and cleanup;
- sandbox, network, and approval policy;
- model and reasoning selection;
- resume, steer, interrupt, cancel, fork, handoff, and archive;
- user-visible task/thread status and navigation.

Kafa Kernel owns:

- requirements, acceptance, task constraints, and failure modes;
- immutable native task packages;
- mapping a Kafa assignment to returned host IDs;
- receipt validation against task, candidate, branch, and controller facts;
- controller verification, integration, and delivery gates;
- audit of host outcomes without pretending to own host lifecycle.

Kafa assignment states remain delivery workflow facts. They must not be
presented as authoritative mirrors of native host lifecycle states.

### Native task package

The root-workspace controller exports a package before asking the host to
create a task or subagent:

```json
{
  "package_version": "1",
  "run_id": "kafa-run-id",
  "assignment_id": "kafa-assignment-id",
  "task_id": "kafa-task-id",
  "cycle_id": "kafa-cycle-id",
  "candidate_sha": "git-or-content-identity",
  "base_ref": "main",
  "target_branch": "agent/task-id",
  "role": "developer",
  "goal": "bounded task goal",
  "acceptance_ids": ["AC-1"],
  "failure_mode_ids": ["FM-1"],
  "test_target_ids": ["UNIT"],
  "file_claims": ["src/example.py"],
  "capability_hints": {
    "risk": "low",
    "task_shape": "small-verified-code-change",
    "requires_sandbox": false,
    "requires_no_network": false
  },
  "package_sha256": "sha256-hex"
}
```

The package contains no SQLite file, connector token, HMAC key, session token,
or mutable runtime directory. The package hash binds the host receipt to the
exact constraints the controller exported.

### Native host receipt

After the host task reaches a relevant outcome, the controlling Skill/host
adapter returns a receipt:

```json
{
  "receipt_version": "1",
  "package_sha256": "sha256-hex",
  "run_id": "kafa-run-id",
  "assignment_id": "kafa-assignment-id",
  "host": {
    "surface": "codex-app",
    "task_id": "native-task-id",
    "thread_id": "native-thread-id",
    "parent_thread_id": "native-parent-thread-id",
    "worktree_id": "native-worktree-id",
    "worktree_path": "/host/reported/path",
    "handoff_id": null
  },
  "policy": {
    "approval_mode": "host-owned",
    "sandbox": "host-owned",
    "network": "host-owned",
    "selected_model": "host-selected",
    "reasoning": "host-selected"
  },
  "status": "completed",
  "branch": "agent/task-id",
  "base_sha": "sha256",
  "head_sha": "sha256",
  "report": {},
  "started_at": "RFC3339",
  "completed_at": "RFC3339",
  "provenance": {
    "kind": "host-attested-or-audit-only",
    "issuer": "host-adapter",
    "payload_sha256": "sha256-hex",
    "signature": "opaque-host-attestation-or-empty"
  }
}
```

Required host identities are real values supplied by the host. Placeholder
values such as `sdk-turn`, generated UUIDs pretending to be native task IDs, or
paths invented by Kafa are rejected.

The receipt is still a raw producer report. It cannot create delivery-eligible
evidence. Controller verification must inspect the actual branch/candidate and
run mapped targets before evidence is trusted.

### Approval and permission authority

Kafa exports constraints such as `requires_sandbox` and
`requires_no_network`; it does not choose the host's approval mode or silently
replace it with `deny_all`, `workspace_write`, or a wider policy.

If the host cannot report the applied sandbox/approval/network policy, the
receipt records it as unknown and any target requiring that property fails
closed. Kafa never infers policy from prompt text.

### Model routing

Kafa emits capability and risk hints, not concrete model slugs. The host policy
chooses model and reasoning and reports the selection in the receipt.

The existing Spark environment policy is legacy compatibility behavior. It
must not become the native default and must not claim deterministic output.
Hard-coded preview model names are removed from the native path.

### Project fact transport

The current product remains root-workspace local for mutable Kernel facts:

- `.ai-team/state/harness.db` has one authoritative writer in the project root;
- managed worktrees and native subagents receive immutable task packages, not
  a copied database;
- workers return code through branches/worktrees and facts through receipts;
- the root controller imports receipts and performs all SQLite mutations;
- `.ai-team/runtime` is not copied as a second source of truth.

Hosted/cloud tasks are unsupported for Kernel mutation until a separately
designed Project Fact Transport provides authenticated package delivery,
receipt return, replay protection, and single-writer import. A hosted task may
edit code, but Kafa cannot call it integrated or verified without the root
controller importing and checking its receipt and branch.

### Legacy providers

`host-codex` becomes an explicit legacy/noninteractive adapter. It is not the
native subagent implementation and must not be selected implicitly. Before it
can be considered operationally safe, it still needs real process-tree
watchdog, liveness, deadline, and cancellation guarantees.

`manual-csv` is an exchange format, not a live provider. Export/import must be
described as controller-mediated task package and receipt exchange; a provider
whose `collect()` can never return a report must not claim a running provider
lifecycle.

## Native flow

```text
Kafa root controller
  -> export immutable task package
  -> Native Codex/ChatGPT creates task/thread/subagent/worktree
  -> host owns approval, model, steer, cancel, handoff
  -> host returns receipt with real IDs
  -> Kafa root imports and validates receipt
  -> controller verifies branch and test targets
  -> Kernel may create trusted evidence
  -> integration and delivery gates remain unchanged
```

## Rejected alternatives

### Keep standalone SDK threads as the default host provider

Rejected because they are not user-visible native subagents and cannot inherit
all parent host policy or lifecycle semantics.

### Copy the live SQLite database into every worktree

Rejected because independent writers create divergent Kernel truth and make
leases, idempotency, gate sequence, and recovery unsafe.

### Let workers mutate the root database directly

Rejected because it couples untrusted producer execution to the control-plane
write authority and reintroduces lock/lifecycle failures.

### Infer native IDs or policy from prompts and paths

Rejected because prompt claims are forgeable. Host identity and policy must be
returned by the host boundary or recorded as unknown.

## Implementation sequence

1. Freeze a native task package and receipt schema with pure validation.
2. Add controller-mediated export/import under existing dispatch ownership.
3. Record real host task/thread/worktree IDs from receipts without creating a
   second host lifecycle state machine.
4. Update Skills to use native Codex thread/task capabilities and return the
   receipt to the root controller.
5. Reclassify `manual-csv` as exchange and `host-codex` as legacy.
6. Replace concrete model routing with capability/risk hints.
7. Add real-host compatibility gates for create, cancel, steer, handoff, and
   worktree receipt import. A skipped live profile is not success.

## Acceptance gates

- Native task/thread/worktree IDs are non-placeholder host values.
- A receipt with wrong package hash, assignment, branch, base/head SHA, or
  candidate is rejected.
- A worker cannot mutate the root SQLite database.
- Host approval/sandbox/network fields are reported, not inferred.
- Cancel, steer, handoff, and archive remain host operations; Kafa only audits
  their receipt/event when supplied.
- Model selection is host-owned and recorded without Kafa choosing a preview
  slug.
- Native reports remain non-evidence until controller verification.
- Hosted tasks without Project Fact Transport fail closed at receipt import.
- Real compatibility tests distinguish pass, fail, and not-run.

## Consequences

Kafa becomes smaller and more truthful: a delivery Kernel around native Codex
rather than a parallel agent platform. The near-term cost is a compatibility
period with explicit legacy providers and a local-only root controller. The
benefit is that user-visible host lifecycle, approval, worktrees, and model
policy have one authority instead of two conflicting implementations.

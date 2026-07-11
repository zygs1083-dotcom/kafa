# Overall Simulation Follow-up Verification

Date: 2026-07-11

Branch: `v1.26-stop-ship-correctness`

Base commit: `a95848b`

Status: the follow-up implementation is verified in the local working tree but
is not committed, pushed, merged, tagged, released, or deployed.

## Reproduced Gaps

An isolated install-to-delivery simulation found three remaining gaps after
the original 28-finding closure:

1. a caller could record `reviewer_context=fresh` with no reviewer session or
   attestation, obtain a passing `local-only` quality gate, and deliver;
2. quickstart emitted bare `harness.py` commands, hard-coded `T1`, and skipped
   the required `qa` phase;
3. a new Delivery Cycle used global evidence presence for its missing-state
   guidance and suggested creating a target instead of reusing the existing
   target.

The false delivery was not detected by runtime doctor, invariant validation,
event validation, or delivery validation. It therefore reopened the complete
QS-001 closure semantics rather than being treated as a documentation-only
problem.

## Fix

- `fresh` now requires both a live reviewer session and its matching
  attestation when the gate is recorded.
- The reviewer session must use a reviewer role and cannot match a known
  producer session for the current Cycle.
- Delivery Decision repeats the session, role, status, attestation, and
  producer-separation checks, so direct storage tampering still fails closed.
- Low/medium local workflows remain available as
  `same-context-degraded`; they no longer claim independent QA.
- The compatibility quality-gate wrapper forwards reviewer session and
  attestation IDs.
- Quickstart commands use the active Python interpreter and absolute resolved
  runtime path, actual task/target/evidence IDs, separate executable commands,
  and the legal phase sequence.
- First-cycle and later-cycle guidance can be executed in order to reach
  delivery without guessing lease/fence values or phase transitions.
- Current-Cycle evidence presence is derived from current-Cycle validation
  links. Candidate evidence suggestions are restricted to the current Cycle's
  dispatch assignment or task attempt, not timestamps or global row counts.

## Regression Coverage

New regressions cover:

- rejecting an unbound fresh gate while accepting a session-bound gate;
- rejecting producer-session reuse even when that session has reviewer-shaped
  identity and an attestation;
- rejecting a tampered stored fresh gate at Delivery Decision;
- executing installed-path quickstart guidance with the real `SMOKE-T1` ID;
- recording an honest degraded gate and following `qa -> delivery_readiness`;
- starting a second Cycle, forcing old evidence to the same timestamp, reusing
  an existing target, and completing the new guided loop;
- keeping README, Runtime, Quickstart, and project-runtime Skill fresh-gate
  examples bound to reviewer identity.
- closing all four leaked test SQLite connections and promoting
  `ResourceWarning` to an error in the final full-suite run.

## Verification

```text
py_compile: passed
validate_structure.py: passed
focused stop-ship/cold-start/docs tests: passed
runtime/schema/delivery group: 104 tests passed
agent E2E/documentation/freeze/control-plane group: 35 tests passed
unittest discover with ResourceWarning promoted to error: 368 tests in 592.501s, OK
SQLite leak regression group: 31 tests in 64.246s, OK
runtime/forward smoke: 3 scenarios passed
skill eval: 18 markers passed
fixture E2E: 5/5 passed
stability E2E: 12/12 passed
  failed_count=0
  false_pass_count=0
  forged_evidence_block_count=1
  sqlite_lock_error_count=0
  human_intervention_count=0
isolated wheel/Plugin install: passed with Codex CLI 0.143.0
real live-codex: 2/2 passed, live_status=passed, skipped=0
```

The isolated installed-runtime simulation additionally observed:

```text
unbound fresh gate: exit 1
guided commands: 5/5 exit 0
stored gate: same-context-degraded / local-only
first cycle: delivered
second cycle old same-timestamp evidence: reported missing
second cycle two-stage guided commands: delivered
```

Python 3.14 emitted no `ResourceWarning` in the warning-strict final suite. The
four leaked test connections found by the first pass now use explicit
`contextlib.closing(...)` ownership.

## Boundary

Manual attestations remain local audit facts. High/critical delivery still
requires connector-origin HMAC trust and a connector-origin reviewer
attestation. This follow-up does not add schema, tables, top-level commands,
Skills, Hooks, connector writes, or a delivery-trust shortcut.

# Wave 4 Connector Data Safety Evidence

Date: 2026-07-10

Branch: `v1.26-stop-ship-correctness`

Status: Wave 4 correctness scope is green. This is not a release, tag, push,
or claim that the Apps/MCP runtime migration is implemented.

## Closed findings

### CN-003: idempotency key did not bind immutable payload

Commit: `1b053fd Bind connector idempotency keys to immutable payloads`

Delivered behavior:

- `adapter_actions.payload_hash` binds tool, mode, artifact, action, and
  canonical JSON payload.
- valid JSON and invalid legacy payloads use separate hash domains.
- same key plus same semantic payload returns the original action without
  mutation or duplicate event.
- same key plus different intent returns `idempotency-conflict`.
- completed actions cannot be reopened or have external IDs/links rewritten.
- empty/mismatched hashes fail closed at transition, execution, recovery, and
  reconciliation boundaries.
- a pre-feature schema 29 table is backfilled only when the column was
  structurally absent; an existing blank value is not silently trusted.
- `adapter plan --request-id` canonicalizes JSON so request and action
  idempotency use the same semantic payload.

Adversarial cases added:

- reordered equivalent JSON;
- conflicting payload reuse;
- completed re-plan and transition mutation;
- database payload tampering;
- blank-hash self-healing attempt;
- invalid-legacy semantic collision;
- completed reconcile bypass;
- structural column upgrade.

### CN-002: Linear comment/update bypassed namespace scope

Commit: `68dfe99 Enforce Linear issue namespace scope`

Delivered behavior:

- Linear comment and update fetch issue metadata before marker search or
  mutation.
- every configured team/project dimension must match the remote issue.
- missing issue metadata, permission-limited metadata, or scope mismatch fails
  closed before a mutation.
- unknown recovery performs the same scope check before marker search.
- intentional `write-confirm` scope override remains audited; `write-auto`
  cannot override.

Adversarial cases added:

- cross-team/project comment and update produce zero mutations;
- matching issue scope permits exactly one mutation;
- missing issue metadata produces zero mutation;
- unknown recovery cannot search or reuse a cross-project object.

### CN-001: Notion ambiguous page create could duplicate

Commit: `094003b Fail closed on ambiguous Notion page creation`

Delivered behavior:

- Notion page create forces the project and idempotency markers into the title
  and an appended Kernel-owned paragraph even with custom caller children.
- a transport disconnect after remote success produces `unknown`.
- retry recovers the existing page by marker and performs no second page POST.
- an `unknown` Notion create with a clear marker miss remains unknown and
  refuses a blind duplicate create.

Adversarial cases added:

- remote success followed by connection close;
- custom children without caller-supplied markers;
- explicit unknown state plus marker miss.

## Apps/MCP boundary

Commit: `fdf36bb Define Apps MCP connector receipt boundary`

The accepted ADR is
[`docs/runtime/APPS_MCP_RECEIPT_ADR.md`](../runtime/APPS_MCP_RECEIPT_ADR.md).
It records the migration direction:

- Apps/MCP owns authorization, workspace policy, tool approval, and external
  protocol execution.
- Kafa owns project scope, immutable intent, outbox fence, receipt validation,
  unknown recovery, and fallback.
- model-visible tool output is audit-only unless the host provides a
  non-forgeable attestation bound to action, fence, payload hash, project key,
  scope, and tool-call ID.
- existing `gh`/HTTP connectors are explicitly `legacy-direct` compatibility
  paths and remain non-delivery evidence.

The OpenAI Codex manual helper failed because the remote response omitted its
expected content hash header. The ADR therefore used verified official-domain
fallback pages for Plugin, MCP customization, and Apps SDK boundaries. It does
not claim that OpenAI currently exposes a generic Kafa receipt API.

## Regression evidence

Targeted connector matrix after all three code fixes:

```text
Ran 43 tests in 55.526s
OK
```

Schema/control-plane targeted checks:

```text
Ran 13 tests in 0.406s
OK
OK: plugin structure is valid
```

Latest full Python regression after the Wave 4 runtime fixes:

```text
Ran 316 tests in 331.911s
OK
```

Fixture runner:

```text
scenario_count=5
passed_count=5
failed_count=0
false_pass_count=0
forged_evidence_block_count=1
sqlite_lock_error_count=0
human_intervention_count=0
```

Stability runner:

```text
scenario_count=12
passed_count=12
failed_count=0
false_pass_count=0
forged_evidence_block_count=1
sqlite_lock_error_count=0
human_intervention_count=0
connector_mock=true
sqlite_stress=true
```

Root doctor:

```text
ok=true
plugin structure=true
control plane contract=true
connector namespace boundary=true
```

Commands:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode fixture
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode stability
kafa doctor --repo . --json
git diff --check
```

## Review evidence and limitations

An independent reviewer reproduced five CN-003 bypass classes during review:
blank-hash self-approval, invalid-legacy collision, completed mutation,
completed reconcile bypass, and request-id canonicalization mismatch. Each was
fixed and given a regression test before commit.

Final independent reruns and later CN-002/CN-001 reviewer tasks could not run
because the Codex account reached its usage limit. This is recorded as a review
infrastructure limitation rather than represented as a pass. Main-agent
adversarial review covered scope override, missing metadata, dual team/project
binding, recovery ordering, custom Notion children, marker placement, and
unknown marker miss.

No live external account/token test was run. All connector requests used fake
`gh` or local HTTP servers. The release contract therefore must not claim live
GitHub/Linear/Notion/Figma/Slack compatibility from this evidence alone.

## Residual risks

- Apps/MCP receipt runtime and real host attestation are designed but not
  implemented.
- Direct adapters remain `legacy-direct` compatibility code.
- Linear installations that cannot return issue team/project metadata will
  fail closed and may require a capability-specific adapter later.
- Notion unknown plus marker miss requires manual resolution by design.
- Live connector behavior remains an opt-in compatibility gate, not ordinary
  CI evidence.
- The repository release state remains `development`; no tag or release was
  created.

## Exit decision

Wave 4 exits green for local connector data safety: concurrent claims,
idempotency intent, cross-project Linear object writes, custom Notion marker
placement, and ambiguous Notion retries are fail-closed under deterministic
tests. Wave 5 may begin native Codex/ChatGPT lifecycle work, but it must not
silently promote legacy connector results or fake compatibility into trusted
receipts.

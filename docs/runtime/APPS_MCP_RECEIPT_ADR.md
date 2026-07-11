# ADR: Apps/MCP Connector Receipt Boundary

- Status: Blocked pending a host-verifiable tool-result receipt; accepted risk for `legacy-direct`
- Date: 2026-07-10
- Last verified: 2026-07-11 against Codex CLI 0.143.0
- Scope: GitHub, Linear, Notion, Figma, and Slack connector execution
- Finding: AP-001

## Context

Kafa currently executes external writes itself: GitHub through `gh api`, and
Linear, Notion, Figma, and Slack through bearer-token HTTP clients. This makes
the Kernel responsible for OAuth/token handling, remote protocol drift, tool
approval, rate limits, workspace policy, and write recovery.

That ownership no longer matches the intended ChatGPT/Codex control plane.
Official Plugin and Apps SDK documentation describes plugins as the
distribution surface and MCP tools as the external capability surface. Kafa's
durable responsibility is narrower: bind an external action to a project,
record immutable intent, prevent duplicate execution, validate the returned
receipt, and keep delivery trust fail-closed.

Official references used for this direction:

- [Build plugins](https://developers.openai.com/codex/plugins/build)
- [Codex customization and MCP](https://developers.openai.com/codex/concepts/customization)
- [Apps SDK](https://developers.openai.com/apps-sdk/)

The `receipt` described below is a Kafa Kernel contract. It is not presented as
an existing generic receipt API from OpenAI.

## Wave 6 Host Capability Finding

The Wave 6 compatibility audit generated the public app-server JSON schemas
from Codex CLI 0.143.0 and inspected a real app-server process. The public
surface proves that Codex owns Apps/MCP discovery, execution, results, and
approval, but it does not expose the host-verifiable connector receipt required
by this ADR.

The relevant public shapes are:

- `mcpServer/toolCall` accepts `threadId`, `server`, `tool`, `arguments`, and
  optional `_meta` in `McpServerToolCallParams`;
- `McpServerToolCallResponse` returns `content`, optional `structuredContent`,
  optional `_meta`, and optional `isError`;
- `McpToolCallThreadItem` exposes a host item `id`, `server`, `tool`,
  `arguments`, `status`, `result`, `error`, `durationMs`, `pluginId`, and App
  context such as `connectorId` and `resourceUri`;
- `item/mcpToolCall/progress` binds progress to `threadId`, `turnId`, and
  `itemId`;
- MCP elicitation and auto-review notifications expose approval lifecycle and
  decisions, but the generated schemas label parts of auto-review as unstable.

Those fields are useful audit correlation, not cryptographic provenance. The
tool-call response and thread item contain no signature, issuer, verification
key, signed payload digest, or binding to Kafa's `action_id`, execution fence,
project key, connector scope, idempotency key, or canonical request hash.

Codex also exposes an opt-in `requestAttestation` initialize capability. It
causes app-server to ask its client for `attestation/generate`, whose response
contains one opaque client token for upstream `x-oai-attestation` use.
`attestation/generate` is not a connector result receipt: the public schema
does not attach that token to an MCP tool result or bind it to any connector
operation, approval, scope, arguments, or returned object.

Therefore a current Apps/MCP result remains audit-only. No Apps/MCP action may
transition a Kafa outbox row to `completed` until a separately reviewed host
adapter can verify a non-forgeable result envelope with all required bindings.
Tool-call IDs, completion status, approval, and model-visible structured output
do not independently satisfy that boundary.

## Current Risk Acceptance

AP-001 is accepted as a bounded compatibility risk for the current development
release; it is not represented as an implemented Apps/MCP migration. The
acceptance has these mandatory conditions:

- the five direct connectors remain explicitly named `legacy-direct`;
- no additional provider-specific direct connector may be added;
- transactional outbox fences, project namespace profiles, double markers,
  ambiguous-outcome recovery, and advisory fallback remain mandatory;
- connector results remain workflow synchronization only and never become
  delivery evidence;
- release evidence must state that Apps/MCP host-attested receipt coverage is
  unavailable on the pinned host contract;
- the capability must be re-evaluated when Codex publishes a result-bound
  attestation or when a separately trusted broker/verifier is designed.

This risk acceptance does not authorize silent success, an unsigned receipt,
or a Kafa-generated substitute for a host signature.

## Decision

### Ownership

ChatGPT Apps/MCP owns:

- connector authorization and OAuth/token custody;
- workspace and administrator policy;
- tool discovery and concrete API protocol;
- user-facing tool approval;
- the external read or write itself;
- provider-specific rate limits and normalized tool output.

Kafa Kernel owns:

- project connector profile and expected external scope;
- immutable operation intent and canonical request hash;
- idempotency key, claim lease, and execution fence;
- receipt validation and local exactly-once state transition;
- unknown/recovery/fallback governance;
- the rule that connector output is not delivery evidence.

The model, Skill, Hook, App, MCP tool, and connector response cannot directly
mark an action completed in Kernel state. Only a receipt accepted by the
Kernel receipt adapter may complete an outbox action.

### Receipt contract

A host adapter must normalize a completed or failed MCP/App tool call into this
logical envelope:

```json
{
  "receipt_version": "1",
  "transport": "chatgpt-app-mcp",
  "provider": "notion",
  "tool_name": "notion.create_page",
  "operation": "notion.page.create",
  "action_id": "uuid",
  "execution_fence": 3,
  "project_key": "project-a",
  "scope": {"parent_page_id": "page-id"},
  "idempotency_key": "stable-key",
  "request_hash": "sha256-hex",
  "status": "succeeded",
  "external_id": "remote-id",
  "external_link": "https://example.invalid/object",
  "started_at": "RFC3339",
  "completed_at": "RFC3339",
  "host": {
    "task_id": "host-task-id",
    "thread_id": "host-thread-id",
    "tool_call_id": "host-tool-call-id",
    "approval": "approved"
  },
  "provenance": {
    "kind": "host-attested",
    "issuer": "host-adapter",
    "payload_sha256": "sha256-hex",
    "signature": "opaque-host-attestation"
  },
  "error": null
}
```

Required binding fields are `action_id`, `execution_fence`, `project_key`,
`scope`, `idempotency_key`, `request_hash`, `operation`, and `tool_call_id`.
The Kernel compares every field with the claimed `adapter_actions` row and the
current connector profile before a CAS transition to `completed`.

The envelope must never contain OAuth tokens, bearer tokens, cookies, API keys,
raw authorization headers, or provider secrets. Raw tool output may be stored
outside SQLite only when redacted; the receipt records its digest, not an
unbounded copy.

### Provenance classes

| Provenance | Kernel action result | Delivery trust |
| --- | --- | --- |
| `host-attested` and valid | May complete matching outbox action | Never delivery evidence |
| `host-attested` but invalid/mismatched | `unknown` or `blocked` | None |
| unsigned MCP/App tool result | Audit-only; cannot complete automatically | None |
| `legacy-direct` HTTP/`gh` result | Existing fenced compatibility path | None |
| model-authored receipt JSON | Rejected | None |

An MCP tool result visible in model context is not inherently trusted. A
receipt becomes host-attested only when its envelope is produced or signed by
a host boundary that the model cannot forge. If a Codex/ChatGPT surface does
not expose such an attestation, its result remains audit-only and requires
controller reconciliation.

### State transitions

```text
planned/confirmed
  -> executing (Kernel claim + fence)
  -> host tool call
  -> receipt validation
     -> completed       valid receipt and CAS succeeds
     -> unknown         remote outcome or receipt binding is ambiguous
     -> blocked         explicit denial, policy failure, or permanent failure
```

The host must use the Kafa idempotency marker when the provider supports a
searchable marker. An `unknown` action first performs a scoped lookup through
the App/MCP capability. A miss does not authorize a blind duplicate create for
operations whose remote success may be ambiguous.

### Scope and approval

Tool approval does not replace project scope validation. Before dispatch, Kafa
checks the requested scope against `connector_profiles`. On receipt, Kafa
checks the actual returned scope again. A host approval for the wrong repo,
team, project, parent page, file, or channel is rejected.

`scope_override` remains a deliberate `write-confirm` exception with a Kernel
finding/event. It may not be silently introduced by an App or MCP tool.

### Legacy mode

The current direct clients are classified as `legacy-direct` compatibility
mode. This ADR does not pretend that Apps/MCP migration already exists and does
not remove the current connector tests.

Until the host receipt adapter ships:

- direct clients remain fenced by the transactional outbox;
- namespace profiles and double markers remain mandatory;
- ambiguous outcomes remain fail-closed;
- direct connector results remain workflow sync only;
- direct tokens remain environment-only and must not enter project state;
- no direct result gains HMAC, controller, validation, or delivery trust.

New connector capabilities should target Apps/MCP receipt mode rather than add
another provider-specific HTTP client to `harness_db.py`.

## Rejected alternatives

### Treat MCP tool output as trusted evidence

Rejected because model-visible output can be replayed, edited, or detached
from the exact action/fence/scope. It also conflates workflow synchronization
with code delivery verification.

### Let Apps/MCP own Kafa project profiles and outbox state

Rejected because external tools do not own Kafa's cycle, candidate, namespace,
or exactly-once invariants. This would move the source of truth outside the
Kernel.

### Keep adding direct HTTP adapters indefinitely

Rejected because it duplicates authorization, policy, approval, protocol, and
rate-limit behavior already owned by the host connector layer.

### Remove direct connectors immediately

Rejected because current Codex/ChatGPT surfaces do not expose one uniform,
documented, host-attested receipt interface across all five providers. An
immediate removal would create a compatibility gap and tempt false success.

## Migration plan

1. Re-run the host capability probe against each pinned Codex release and
   require a documented verifier plus result-bound attestation fields.
2. Define a versioned receipt JSON schema and pure validator without changing
   delivery trust only after the host contract can supply the required
   bindings.
3. Implement one Apps/MCP receipt adapter behind an explicit compatibility
   profile, starting with a provider whose tool result exposes stable IDs.
4. Run dual-path evals that compare direct and Apps/MCP scope, idempotency, and
   recovery behavior without issuing duplicate writes.
5. Make Apps/MCP receipt mode preferred only after real-host compatibility
   gates pass; keep `legacy-direct` explicit during deprecation.
6. Remove provider token handling and direct protocol code only after all five
   providers have verified receipt/recovery coverage.

Any runtime implementation that persists receipts requires a separately
reviewed schema/CLI plan. This ADR alone adds no runtime command, table, trust
shortcut, or claim that Apps/MCP migration is complete.

## Acceptance gates for implementation

- A receipt with a wrong action, fence, payload hash, project key, or scope is
  rejected before local completion.
- Replaying a receipt is idempotent; using it for another action conflicts.
- Unsigned/model-authored receipts cannot complete actions.
- Host denial and missing approval fail closed.
- Ambiguous remote success performs scoped recovery before any retry.
- Cross-project marker and object reuse is rejected.
- Connector receipt and fallback artifacts never satisfy delivery readiness.
- Fake tests are supplemented by an opt-in real Codex/ChatGPT Apps/MCP gate;
  skipped live tests are not counted as compatibility success.

## Consequences

The architecture becomes simpler over time: Apps/MCP handles external systems,
while Kafa remains the policy and audit Kernel. The cost is a staged migration
and a period where `legacy-direct` and receipt paths coexist. Release claims
must state which providers have real host receipt coverage and which remain
legacy compatibility paths.

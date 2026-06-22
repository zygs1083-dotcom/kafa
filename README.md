# Codex Project Harness (Code Delivery Architecture v2)

Codex Project Harness is a Codex-native project operating system that orchestrates verified code delivery using executable local runtime state, structured workflows, dynamic team generation, collaboration-tool mapping, and skill-based execution.

This version introduces a **3-layer execution model** and a **Failure Mode Engineering system** for delivering code with clear requirements, tests, independent QA, and handoff evidence.

The harness stops at code delivery. It does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation.

It can use Git/GitHub, Linear, Notion, Figma, and Slack during delivery when useful. Codex decides which tools are needed from project context, with local `.ai-team/` and `docs/harness/` files as fallback. High-impact external actions still require confirmation.

---

# Core Architecture Upgrade

## 1. Three-Layer Execution Model

### Layer 0 — Project Manager (Single Source of Truth)
- Entry point for all projects
- Responsible for requirement clarification and orchestration
- Maintains global state and decisions
- Assigns work to domain sessions

### Layer 1 — Domain Sessions (Role-Based Contexts)
- Product / Dev / QA / Security / Ops sessions
- Each session owns a domain of reasoning
- No direct execution of unrelated domains
- Produces structured outputs and task breakdowns

### Layer 2 — Subagents (Task Execution Units)
- Fine-grained execution agents
- Each handles a single task or subtask
- Stateless, short-lived execution
- Must return verifiable artifacts

---

## 2. Execution Flow

1. Project Manager receives idea
2. Runs project bootstrap: workspace, git, harness files, and useful collaboration tools
3. Updates executable runtime state and task records
4. Performs requirement baseline and clarification
5. Generates or selects team architecture
6. Creates domain sessions (Product / Dev / QA etc.)
7. Domain sessions break work into subagents
8. Subagents execute isolated tasks
9. QA and validation run independently
10. Results are aggregated by Project Manager
11. Delivery readiness + code handoff

---

## 3. Failure Mode Engineering (NEW)

Every feature must explicitly model:

- Normal path (happy flow)
- Edge cases
- Invalid input scenarios
- Concurrency issues
- Partial failure states
- Recovery strategies
- Reversal or local rollback plan

### Required Output Artifact
Each feature must generate:

```
Failure Mode Matrix
- Scenario
- Trigger
- Expected system behavior
- Recovery strategy
- Data safety guarantee
- Test coverage mapping
```

---

## 4. Subagent Requirements Upgrade

Subagents must now include:

- Explicit failure assumptions
- Retry behavior
- Idempotency guarantees
- Reversal awareness
- Test linkage

---

## 5. Design Principles

- No single agent owns full lifecycle decisions
- Separation of reasoning (domain sessions) and execution (subagents)
- Failure is first-class, not optional
- All flows must be reversible or recoverable
- Delivery ends with verified code and evidence, not deployment

---

## 6. Compatibility

This upgrade is fully compatible with existing skills:
- project-harness
- project-bootstrap
- project-runtime
- team-architecture
- minimal-safe-change
- test-first-delivery
- bug-fix-loop
- delivery-readiness
- independent-quality-gate

See `examples/full-project-flow.md` for a complete request-to-delivery walkthrough and `examples/forward-tests.md` for fresh-session validation prompts.

---

# End of Extension

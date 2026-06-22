# Codex Project Harness (Code Delivery Architecture v2)

Codex Project Harness is a Codex-native, general-purpose project operating system that orchestrates verified code delivery using executable local runtime state, structured workflows, dynamic team generation, collaboration-tool mapping, and skill-based execution.

This version introduces a **3-layer execution model**, official Codex plugin metadata, executable runtime checks, a **Failure Mode Engineering system**, and an independent quality-gate contract for delivering code with clear requirements, tests, independent QA, and handoff evidence.

The harness stops at code delivery. It does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation.

It can use Git/GitHub, Linear, Notion, Figma, and Slack during delivery when useful. Codex decides which tools are needed from project context, with local `.ai-team/` and `docs/harness/` files as fallback. External tools are adapters, not prerequisites. High-impact external actions still require confirmation.

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
- ID
- Feature
- Scenario
- Trigger
- Expected system behavior
- Recovery strategy
- Data safety guarantee
- Risk level
- Test coverage mapping
```

Runtime artifact:

```text
.ai-team/requirements/failure-modes.md
```

Use:

```bash
python3 plugins/codex-project-harness/scripts/add_failure_mode.py --id FM1 --feature "Feature" --scenario "Scenario" --trigger "Trigger" --expected "Expected behavior"
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

## 5. Independent Quality Gate

Before handoff, QA records must identify:

- reviewed commit or revision,
- reviewer context: `fresh`, `same-context-degraded`, or `external`,
- result: `pass`, `fail`, `conditional`, or `blocked`,
- blocking findings,
- commands and evidence,
- residual risk.

Runtime artifact:

```text
docs/harness/quality-gates.md
```

Use:

```bash
python3 plugins/codex-project-harness/scripts/record_quality_gate.py --commit HEAD --reviewer-context same-context-degraded --result pass --commands "test command"
```

---

## 6. Collaboration Tool Policy

Tool usage is universal and context-sensitive:

- Use local harness files for every project.
- Use Git/GitHub when repo, PR, issue, checks, or review context exists.
- Use Linear when task tracking is useful.
- Use Notion when shared PRD, decision, QA, or handoff notes are useful.
- Use Figma when design context or visual acceptance matters.
- Use Slack for coordination drafts or confirmed team messages.

Adapter modes:

```text
off -> read-only -> draft-write -> write-confirm -> write-auto
```

High-impact, public, destructive, paid, permission-changing, or production-affecting actions must not run automatically.

---

## 7. Design Principles

- No single agent owns full lifecycle decisions
- Separation of reasoning (domain sessions) and execution (subagents)
- Failure is first-class, not optional
- All flows must be reversible or recoverable
- External tools enrich the flow but do not replace local state
- Delivery ends with verified code and evidence, not deployment

---

## 8. Compatibility

This is a v2 plugin-format upgrade. Install the whole plugin directory so skills can share plugin-level `scripts/`, `references/`, `templates/`, and `schemas/`.

Included skills:
- project-harness
- project-bootstrap
- project-runtime
- requirement-baseline
- team-architecture
- minimal-safe-change
- test-first-delivery
- bug-fix-loop
- delivery-readiness
- independent-quality-gate

See `examples/full-project-flow.md` for a complete request-to-delivery walkthrough and `examples/forward-tests.md` for fresh-session validation prompts.

---

# End of Extension

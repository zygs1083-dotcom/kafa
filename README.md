# Codex Project Harness (Extended Architecture v2)

Codex Project Harness is a Codex-native project operating system that orchestrates end-to-end software delivery using structured workflows, dynamic team generation, and skill-based execution.

This version introduces a **3-layer execution model** and a **Failure Mode Engineering system** inspired by agent workflow research and production systems.

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
2. Performs requirement baseline and clarification
3. Generates or selects team architecture
4. Creates domain sessions (Product / Dev / QA etc.)
5. Domain sessions break work into subagents
6. Subagents execute isolated tasks
7. QA and validation run independently
8. Results are aggregated by Project Manager
9. Release readiness + deployment approval

---

## 3. Failure Mode Engineering (NEW)

Every feature must explicitly model:

- Normal path (happy flow)
- Edge cases
- Invalid input scenarios
- Concurrency issues
- Partial failure states
- Recovery strategies
- Rollback plan

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
- Rollback awareness
- Test linkage

---

## 5. Design Principles

- No single agent owns full lifecycle decisions
- Separation of reasoning (domain sessions) and execution (subagents)
- Failure is first-class, not optional
- All flows must be reversible or recoverable

---

## 6. Compatibility

This upgrade is fully compatible with existing skills:
- project-harness
- team-architecture
- minimal-safe-change
- test-first-delivery
- bug-fix-loop
- release-readiness
- independent-quality-gate

---

# End of Extension

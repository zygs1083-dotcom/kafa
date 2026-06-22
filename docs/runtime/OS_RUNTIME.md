# Codex OS Runtime Layer v0.1

This document defines the initial runtime layer for Codex OS inside the kafa system. It formalizes execution primitives that connect the existing Harness methodology with an operational code-delivery layer.

---

# 1. Purpose

The runtime layer introduces four core execution primitives:

- Project Bootstrap (workspace and collaboration control plane)
- Task Scheduler (control flow)
- State Machine (system state tracking)
- Event Bus (system communication)

These components transform the current Harness from a methodology into a partially executable code-delivery operating model.

The runtime stops at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are out of scope.

---

# 2. Project Bootstrap

## Responsibility

The bootstrap layer determines:

- Whether git exists and whether a branch is needed
- Whether `.ai-team/` and `docs/harness/` exist
- Whether GitHub, Linear, Notion, Figma, or Slack should be used
- Which artifact is the source of truth for requirements, tasks, design, validation, and delivery

## Rules

- Local harness files are always a fallback
- Codex decides which tools are useful from context
- High-impact external actions require confirmation
- Missing external tools do not block local code delivery

---

# 3. Task Scheduler (Core Orchestrator)

## Responsibility

The scheduler determines:

- What task should run
- In what order tasks execute
- Which agent is responsible
- What dependencies must be satisfied

## Model

```text
Task = {
  id,
  type,
  priority,
  dependencies,
  assigned_agent,
  status
}
```

## Rules

- No task executes without explicit scheduling
- Dependencies must be resolved before execution
- Parallel execution allowed only if no dependency conflict exists

---

# 4. State Machine

## Purpose

Provides a single source of truth for task lifecycle.

## States

```text
created -> bootstrapped -> planned -> in_progress -> testing -> review -> delivery_ready -> archived
```

## Rules

- State transitions must be explicit
- No implicit state changes allowed
- Every state change must be logged

---

# 5. Event Bus

## Purpose

Enables decoupled communication between agents and system components.

## Event Model

```text
Event = {
  id,
  type,
  source,
  target,
  payload,
  timestamp
}
```

## Core Events

- task_created
- project_bootstrapped
- task_assigned
- task_started
- task_completed
- task_failed
- review_requested
- review_completed
- delivery_ready

## Rules

- All state transitions emit events
- Events are immutable
- Events are the only communication mechanism between agents

---

# 6. Integration with Existing Harness

This runtime layer extends the existing system:

- project-harness -> becomes entry point into runtime
- project-bootstrap -> checks workspace and collaboration control plane
- team-architecture -> maps agents to scheduler assignments
- skills -> become executable behaviors triggered by events

---

# 7. Execution Flow

```text
User Request
  ↓
project-harness
  ↓
project-bootstrap
  ↓
Task Scheduler
  ↓
State Machine
  ↓
Agent Execution (Skills)
  ↓
Event Bus updates
  ↓
QA / Delivery / Feedback loop
```

---

# 8. Constraints

- No task executes outside scheduler
- No state mutation without event emission
- No agent operates without assignment
- No deployment or production operation executes inside this runtime

---

# 9. Version

v0.1 (Initial runtime abstraction layer)

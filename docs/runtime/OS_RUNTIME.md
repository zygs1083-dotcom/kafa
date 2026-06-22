# Codex OS Runtime Layer v0.1

This document defines the initial runtime layer for Codex OS inside the kafa system. It formalizes execution primitives that connect the existing Harness methodology with an operational system layer.

---

# 1. Purpose

The runtime layer introduces three core execution primitives:

- Task Scheduler (control flow)
- State Machine (system state tracking)
- Event Bus (system communication)

These components transform the current Harness from a methodology into a partially executable operating model.

---

# 2. Task Scheduler (Core Orchestrator)

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

# 3. State Machine

## Purpose

Provides a single source of truth for task lifecycle.

## States

```text
created → planned → in_progress → testing → review → done → archived
```

## Rules

- State transitions must be explicit
- No implicit state changes allowed
- Every state change must be logged

---

# 4. Event Bus

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
- task_assigned
- task_started
- task_completed
- task_failed
- review_requested
- review_completed

## Rules

- All state transitions emit events
- Events are immutable
- Events are the only communication mechanism between agents

---

# 5. Integration with Existing Harness

This runtime layer extends the existing system:

- project-harness → becomes entry point into runtime
- team-architecture → maps agents to scheduler assignments
- skills → become executable behaviors triggered by events

---

# 6. Execution Flow

```text
User Request
  ↓
project-harness
  ↓
Task Scheduler
  ↓
State Machine
  ↓
Agent Execution (Skills)
  ↓
Event Bus updates
  ↓
QA / Release / Feedback loop
```

---

# 7. Constraints

- No task executes outside scheduler
- No state mutation without event emission
- No agent operates without assignment

---

# 8. Version

v0.1 (Initial runtime abstraction layer)

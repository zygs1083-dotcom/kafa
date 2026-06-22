---
name: "requirement-baseline"
description: "Use when requirements are vague, changing, or need to be turned into a confirmed code-delivery baseline with acceptance criteria, non-goals, risks, and traceability before implementation. Use before building a project or feature when Codex must clarify scope with the user before producing code."
---

# Requirement Baseline

Turn an idea into an executable requirement baseline.

## Steps

1. Identify the product goal, users, scenarios, and success criteria.
2. Separate must-have, should-have, could-have, and explicit non-goals.
3. Convert vague needs into observable acceptance criteria.
4. Identify data, permission, integration, operational, and compliance constraints.
5. Record assumptions and open questions.
6. Ask only high-leverage questions; continue with stated assumptions when safe.
7. Freeze a baseline before implementation when scope is broad.
8. Map the baseline to useful collaboration tools when context supports it:
   - Notion for PRD and decision records.
   - Linear or GitHub issues for scope, tasks, and acceptance checklists.
   - Figma for design inputs, prototypes, component constraints, and visual acceptance.
   - Slack only for clarification summaries when useful and appropriate.

## Delivery Boundary

Define what code and artifacts will be delivered. Exclude deployment, production release, infrastructure provisioning, production data migration, secret changes, and paid-resource creation unless a different workflow explicitly takes over.

## Tool Integration

Codex should decide whether external tools are useful. Use local harness files as fallback.

- If Notion context is available or useful, mirror the baseline as a PRD or requirements note.
- If Linear or GitHub issue tracking is useful, map each acceptance criterion to an issue/task/checklist item.
- If Figma is relevant, record design links, frames, unresolved design questions, and visual acceptance criteria.
- If Slack would reduce coordination friction, prepare a summary; ask before sending to a channel or person.

## Confirmation Gate

For broad or ambiguous work, ask the user to confirm the baseline before implementation:

```text
我理解本阶段要交付的是：
- 目标：
- 用户/场景：
- 必须实现：
- 暂不实现：
- 验收标准：
- 风险和待确认：

请确认或修正以上范围。确认后我会按这个基线拆任务并开始实现。
```

## Output

Use this structure:

```text
# Requirement Baseline

## Goal
## Users
## Scenarios
## Functional Requirements
## Non-Functional Requirements
## Acceptance Criteria
## Non-Goals
## Delivery Boundary
## Tool Mapping
## Assumptions
## Open Questions
## Risks
## Traceability
```

## Rule

No implementation task is complete unless it maps back to at least one acceptance criterion.

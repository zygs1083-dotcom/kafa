# Full Project Flow Example

This example shows how `project-harness` should behave when a user asks Codex to build a new feature or project. The flow stops at verified code delivery.

## User Request

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

## 1. Intake

Codex should inspect the workspace, identify whether this is a new project or an existing codebase, and summarize the initial understanding.

Expected output:

```text
我理解你想交付一个亲友关系管理小程序，核心包括亲友档案、生日提醒、关系图谱。
我会先形成需求基线，确认范围后再拆任务和实现。
```

## 2. Project Bootstrap

Codex should decide whether Git, GitHub, Linear, Notion, Figma, or Slack is useful from context. Local harness files remain the fallback.

Expected output:

```text
# Project Bootstrap

## Workspace
- Existing project or new project:
- Existing instructions:

## Git
- Repository:
- Branch:
- Remote:
- Recommended branch:

## Harness Files
- .ai-team:
- docs/harness:

## Collaboration Tools
- GitHub:
- Linear:
- Notion:
- Figma:
- Slack:

## Source Of Truth
- Requirements:
- Tasks:
- Design:
- Validation:
- Delivery:

## Next Step
- Build and confirm requirement baseline.
```

For example, if a GitHub remote exists and the task is large, Codex may use GitHub issues or a draft PR. If no Linear/Notion/Figma/Slack context is present, Codex should continue with local harness files instead of stopping.

## 3. Requirement Baseline

Codex should produce a baseline and ask for confirmation when scope is broad.

```text
我理解本阶段要交付的是：
- 目标：交付一个可运行的小程序基础版本。
- 用户/场景：个人用户维护亲友信息，查看生日提醒和关系图。
- 必须实现：亲友档案 CRUD、生日字段、近期生日列表、基础关系图展示。
- 暂不实现：云端同步、社交邀请、复杂权限、部署上线。
- 验收标准：能新增/编辑/删除亲友；能看到 30 天内生日；能查看基础关系图；关键流程有测试或可执行验证。
- 工具映射：本地 `.ai-team` 作为默认事实源；如检测到 GitHub/Linear/Notion/Figma 上下文则自动映射。
- 风险和待确认：数据存储方式、提醒方式、关系图复杂度。

请确认或修正以上范围。确认后我会按这个基线拆任务并开始实现。
```

## 4. Team Architecture

Use the smallest effective team.

```text
Selected Patterns:
- Supervisor
- Producer-Reviewer
- Pipeline

Agents:
- Bootstrap Coordinator: inspect workspace, git, harness files, and useful collaboration tools.
- Product Analyst: clarify scenarios and acceptance criteria.
- Architect: define data model and module boundaries.
- Developer: implement scoped code changes.
- QA Reviewer: independently validate behavior and integration consistency.
- Delivery Coordinator: package delivery evidence.
```

## 5. Planning

Codex should break confirmed scope into implementation tasks tied to acceptance criteria.

```text
Task Board:
- T1 Data model and local storage | Tool link: local or Linear/GitHub
- T2 Relative profile CRUD | Tool link: local or Linear/GitHub
- T3 Birthday reminder list | Tool link: local or Linear/GitHub
- T4 Relationship graph view | Tool link: local or Linear/GitHub/Figma
- T5 Tests and validation | Tool link: local or GitHub checks
- T6 Independent QA | Tool link: local or GitHub review/Notion note
- T7 Delivery readiness | Tool link: local or GitHub PR/Notion handoff
```

## 6. Implementation

Implementation should follow local project conventions and keep changes scoped.

Each producer output should include:

```text
Role:
Task:
Input:
Decision:
Output:
Evidence:
Tool Links:
Risks:
Next:
```

## 7. Independent QA

For broad changes, split QA into short-lived subagents:

```text
QA-A: API/data contract and validation
QA-B: UI flows and empty/loading/error states
QA-C: data persistence and failure modes
QA-D: permission/security review when relevant
```

Each QA subagent must return findings, evidence, and residual risk.

## 8. Delivery Readiness

Final output should package the code delivery:

```text
# Delivery Readiness

## Scope
## Acceptance Mapping
## Changed Files
## Validation
## Independent QA
## Collaboration Links
## Data / Config Notes
## Known Gaps
## Handoff Notes
## Out Of Scope
- Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation.
```

## 9. Retrospective

After delivery, Codex may summarize what should improve in the harness:

```text
Wins:
Problems:
Root Causes:
Process Changes:
Skill / Agent Changes:
Tooling Changes:
Follow-Up Tasks:
```

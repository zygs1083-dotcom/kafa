---
name: "project-harness"
description: "Use when the user wants to develop, create, build, launch, or fully deliver a software/data/automation project with Codex. Orchestrates requirements, team architecture, implementation, QA, release readiness, and retrospective."
---

# Project Harness

You are the project manager and orchestrator for the whole delivery.

## Trigger

Use this skill for requests like:

- 我要开发/创建/搭建一个项目
- 从零到上线
- 帮我完整实现这个系统
- 组建 Agent 小队完成项目
- Build this project end to end

## Workflow

1. Inspect the workspace and current repository state.
2. Clarify only missing information that materially changes scope, risk, or acceptance.
3. Build a requirement baseline with acceptance criteria and non-goals.
4. Ask for confirmation before treating the baseline as execution scope when the project is ambiguous or high impact.
5. Initialize the control plane with `scripts/init_project_harness.py` when appropriate.
6. Use `team-architecture` logic to choose the smallest effective agent team.
7. Dispatch work with clear owners, inputs, outputs, and verification evidence.
8. Keep producer and reviewer roles separate.
9. Use a maximum of two producer-reviewer retry loops before escalating.
10. Run integration coherence QA before declaring completion.
11. Require explicit human approval for production deployment, irreversible migration, secret changes, or paid-resource creation.
12. Finish with a concise delivery report and update the evolution log when useful.

## Control Files

Create or maintain these when the project is substantial:

```text
.ai-team/control/project-charter.md
.ai-team/control/project-state.yaml
.ai-team/control/agent-registry.md
.ai-team/requirements/requirements.md
.ai-team/requirements/acceptance.md
.ai-team/planning/task-board.md
docs/harness/team-architecture.md
docs/harness/validation.md
docs/harness/evolution-log.md
```

Do not preserve noisy raw run logs unless they are needed for debugging or audit.

## Output Contract

Every major phase should leave:

- decision made,
- current scope,
- owner,
- evidence,
- remaining risk,
- next action.

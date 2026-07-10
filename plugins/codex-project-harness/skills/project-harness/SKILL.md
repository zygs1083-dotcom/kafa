---
name: "project-harness"
description: "Use when the user wants to develop, create, build, implement, or fully deliver code for a software/data/automation project with Codex, including Chinese requests like 我要开发, 帮我做一个, 实现一个功能, 搭建一个系统, 从0到代码交付. Orchestrates executable project runtime updates, project bootstrap, git/workspace checks, Codex-selected GitHub/Linear/Notion/Figma/Slack collaboration mapping, requirement clarification, confirmed scope, team architecture, implementation, tests, independent QA, and code delivery handoff. This skill stops at delivery of verified code and does not perform deployment, production release, infrastructure provisioning, production migrations, secret changes, or paid-resource creation."
---

# Project Harness

You are the project manager and orchestrator for code delivery.

## Trigger

Use this skill for requests like:

- 我要开发/创建/搭建一个项目
- 帮我做一个功能/系统
- 实现一个功能
- 从零交付可验收代码
- 帮我完整实现这个系统
- 组建 Agent 小队完成项目
- Build and deliver this project end to end

Do not use this skill to deploy, release to production, provision paid/cloud resources, rotate secrets, or run irreversible production operations. If the user asks for those actions, stop at a delivery handoff and state that deployment is outside this harness.

## Delivery Boundary

The harness owns:

- requirement baseline and acceptance criteria,
- project bootstrap, git/workspace checks, and collaboration tool mapping,
- executable runtime state, tasks, validation records, and delivery records,
- implementation plan and task routing,
- code changes and local/project tests,
- independent QA and integration coherence review,
- delivery package with evidence, residual risks, and next-step notes.

The harness does not own:

- production deployment or release approval,
- infrastructure provisioning,
- production database migrations,
- secret or credential changes,
- paid-resource creation,
- post-release monitoring operations.

## Phase State Machine

Move through these phases explicitly:

```text
intake -> project_bootstrap -> requirement_baseline -> confirmation -> team_architecture -> planning -> implementation -> qa -> delivery_readiness -> retrospective
```

Rules:

- Run `project_bootstrap` before requirement baselining for new projects, substantial features, or any request that mentions GitHub, Linear, Notion, Figma, Slack, issues, PRs, design, or team coordination.
- Use `project-runtime` scripts whenever a phase, task, decision, validation record, or delivery record changes.
- Do not enter `implementation` before a requirement baseline exists unless the request is a narrow, already-clear change.
- Ask for confirmation before freezing the baseline when scope, data model, user workflow, or acceptance criteria are ambiguous.
- Skip `team_architecture` only for small changes where one producer and one review pass are enough.
- Always run `qa` before claiming delivery.
- Stop at `delivery_readiness`; do not continue into deployment.

## Intake Classification

Classify the request before choosing the full path:

| Class | Route |
| --- | --- |
| Explanation, translation, or summary only | Answer directly; do not enter harness |
| Small clear code change | `minimal-safe-change`, with bootstrap only if git/tooling state matters |
| Bug or failing behavior | `bug-fix-loop`, with bootstrap only if issue/branch/PR context matters |
| Clear feature | Lightweight `project-bootstrap` + `requirement-baseline` + implementation + QA |
| Broad or vague project | Full phase state machine |
| Deployment or production operation | Stop at code delivery boundary; do not execute deployment |

## Communication Gates

Ask concise, high-leverage questions when the answer materially changes scope, data shape, permissions, irreversible behavior, or acceptance.

Use this baseline confirmation shape before implementation on broad work:

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

## Work Method Discipline

Before implementation:

- Restate the root problem the task is meant to solve.
- Split the work into the smallest units that can be independently verified.
- Explain why key decisions are being made, not only how to implement them.

Before delivery:

- Run an adversarial review against logic gaps, incorrect facts, simpler alternatives, and verification evidence.
- List the most likely remaining failure points when risk remains.
- Do not claim the work is complete based on "looks good"; provide verification evidence or explicit residual risk.

## Workflow

1. Inspect the workspace and current repository state.
2. Run `project-bootstrap` when the work is new, substantial, or tool-coordinated.
3. Use `project-runtime` to update the current phase and local control plane.
4. Clarify only missing information that materially changes scope, risk, or acceptance.
5. Build a requirement baseline with acceptance criteria and non-goals.
6. Identify failure modes for risky behavior and map them to acceptance criteria or explicit exemptions.
7. Ask for confirmation before treating the baseline as execution scope when the project is ambiguous or high impact.
8. Initialize the control plane with `scripts/init_project_harness.py` when appropriate.
9. Use `team-architecture` logic to choose the smallest effective agent team.
10. Add tasks through `project-runtime` with owners, acceptance mapping, failure-mode mapping, tool mapping, dependencies, and evidence fields.
11. Before dispatching native subagent work, run `dispatch route-advice` for capability/risk hints; the host owns the concrete model, reasoning, sandbox, approval, and task/thread/worktree lifecycle.
12. Use `dispatch native-export` to produce immutable packages, create visible native host tasks/subagents, and return real host task/thread/worktree IDs through `dispatch native-import`. Provider reports remain raw until controller verification.
13. Keep producer and reviewer roles separate.
14. Use a maximum of two producer-reviewer retry loops before escalating.
15. Run integration coherence QA before declaring completion.
16. Record QA, quality-gate, and delivery evidence through `project-runtime`.
17. Use `delivery-readiness` to package verified code, tests, changed files, residual risks, tool handoff links, and notes.
18. Finish with a concise delivery report and update the evolution log when useful.

## Skill Routing

Route work through the smallest useful path:

| Situation | Skill |
| --- | --- |
| New/substantial project, repo setup, branch setup, GitHub/Linear/Notion/Figma/Slack coordination | `project-bootstrap` |
| Phase/task/decision/validation/delivery state changes | `project-runtime` |
| Broad or vague new project / feature | `requirement-baseline` first |
| Agent roles or parallel work are useful | `team-architecture` |
| Small focused patch | `minimal-safe-change` |
| New behavior or contract-sensitive change | `test-first-delivery` |
| Reported defect or failing behavior | `bug-fix-loop` |
| Finished implementation needs review | `independent-quality-gate` |
| Code is ready to hand off | `delivery-readiness` |
| Harness files or team state drift | `harness-audit` |
| Milestone completed or process needs improvement | `project-retrospective` |

## Collaboration Tools

Use `references/collaboration-tools.md` when the project uses or requests GitHub, Linear, Notion, Figma, Slack, issues, PRs, design files, or status notifications.
Use `references/tool-adapters.md` when deciding whether and how to sync local harness records to GitHub, Linear, Notion, Figma, or Slack.

Default source-of-truth policy:

- Local fallback: `.ai-team/` and `docs/harness/`.
- Git/GitHub: code state, branches, PRs, review, checks, and issue links.
- Linear: task/project tracking when useful or already used by the project.
- Notion: PRD, decisions, architecture notes, QA notes, and delivery records when useful.
- Figma: design context, prototypes, component references, and visual acceptance when relevant.
- Slack: progress updates, review requests, and delivery handoff only after confirmation.

Codex should decide whether each tool is needed. Ask only before high-impact external actions such as Slack messages, public/shared artifact creation, permission or secret changes, paid resources, destructive edits, or production-related changes. Reading external context and low-risk project-management writes can proceed when the target and purpose are clear.

## Session And Subagent Model

- Layer 0 Project Manager is the controlling conversation and single source of truth.
- Layer 1 Domain Sessions are role-based contexts such as Product, Architecture, Development, QA, Security, and Delivery. Use separate sessions when the runtime supports them; otherwise emulate them with clearly labeled role outputs in the same conversation.
- Layer 2 Subagents are short-lived task execution units. They may be spawned inside a domain session for independent checks such as QA-A API contract, QA-B UI behavior, and QA-C data/schema safety. They do not need independent user-visible sessions unless the runtime provides them.
- Every subagent returns a verifiable artifact, not just an opinion.
- Treat `native-host-small-verified` as a capability hint for a small, low-risk, controller-verifiable developer task; the native host owns the concrete model and reasoning policy.
- Require stronger host policy or main-model/manual review for architecture, QA judgment, high/critical failure modes, sandbox/no-network targets, missing-target work, broad refactors, or ambiguous tasks.
- If no native candidate exists, state that clearly and keep the work with the controlling model or manual flow.

## Control Files

Create or maintain these when the project is substantial:

```text
.ai-team/control/project-charter.md
.ai-team/control/project-state.yaml
.ai-team/control/agent-registry.md
.ai-team/control/capability-report.md
.ai-team/control/tooling-map.md
.ai-team/control/decision-log.md
.ai-team/requirements/requirements.md
.ai-team/requirements/acceptance.md
.ai-team/requirements/failure-modes.md
.ai-team/requirements/traceability.md
.ai-team/planning/task-board.md
docs/harness/bootstrap.md
docs/harness/team-architecture.md
docs/harness/validation.md
docs/harness/quality-gates.md
docs/harness/delivery.md
docs/harness/evolution-log.md
```

Do not preserve noisy raw run logs unless they are needed for debugging or audit.

## Output Protocol

Use this shape for role or subagent returns:

```text
Role:
Task:
Input:
Decision:
Output:
Evidence:
Risks:
Next:
```

## Output Contract

Every major phase should leave:

- decision made,
- current scope,
- owner,
- evidence,
- remaining risk,
- next action.

Final delivery must include:

- delivered behavior mapped to acceptance criteria,
- changed files or modules,
- tests/checks run and results,
- independent QA findings,
- failure-mode coverage or exemption reason,
- quality-gate result, reviewed commit/revision, and reviewer context,
- GitHub/Linear/Notion/Figma/Slack links or fallback local artifacts used,
- known gaps or residual risks,
- explicit note that deployment is not included.

Before final delivery, run:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . status
python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
```

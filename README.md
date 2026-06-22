# Codex Project Harness

Codex Project Harness is a Codex-native project team operating system. It turns large software requests into a controlled workflow with requirement baselines, generated agent teams, reusable skills, quality gates, release readiness checks, and retrospectives.

The design borrows the useful ideas from harness-style agent systems, but keeps the implementation aligned with Codex:

- Codex Skills instead of a single oversized methodology document
- Codex subagents and `.codex/agents/` instead of Claude-specific agent paths
- Project control files under `.ai-team/`
- Progressive disclosure through `references/`
- Small reusable skills only when they have independent trigger value

## Skills

| Skill | Purpose |
| --- | --- |
| `project-harness` | Start and orchestrate a full project from idea to delivery |
| `requirement-baseline` | Clarify, freeze, and trace requirements |
| `team-architecture` | Generate the right agent team and project-specific skills |
| `minimal-safe-change` | Make the smallest complete change with verification |
| `test-first-delivery` | Deliver with tests, contracts, and regression checks |
| `bug-fix-loop` | Reproduce, diagnose, fix, and prevent a bug from returning |
| `independent-quality-gate` | Run independent QA, review, and integration coherence checks |
| `release-readiness` | Prepare deployment, migration, rollback, and monitoring evidence |
| `harness-audit` | Audit drift in agents, skills, rules, and control files |
| `project-retrospective` | Convert project evidence into reusable process improvements |

## Repository Layout

```text
plugins/codex-project-harness/
├── .codex-plugin/plugin.json
├── skills/
├── docs/
├── scripts/
└── templates/
```

## Quick Start

Install the plugin or copy `plugins/codex-project-harness/skills/*` into a Codex skills directory. Then start a project with natural language:

```text
我要开发一个跨境电商商品主数据系统，请从需求到上线完整推进。
```

For explicit invocation:

```text
$project-harness
```

Use smaller skills directly when the task is narrow:

```text
$minimal-safe-change
请用最小改动修复这个筛选条件 bug，并补回归测试。
```

## Safety Defaults

- Requirement baseline before execution when scope is unclear
- Human approval before production deployment or irreversible operations
- Producer and reviewer are separated
- Maximum producer-reviewer retry count is 2 before escalation
- No default creation of excessive agents
- No automatic secret writes or paid-resource creation

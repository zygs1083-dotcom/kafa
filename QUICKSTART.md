# Quick Start

## Full Project

Say:

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

The `project-harness` skill should:

1. inspect the workspace,
2. bootstrap git, harness files, and useful GitHub/Linear/Notion/Figma/Slack mappings,
3. update runtime phase and task state through `project-runtime`,
4. clarify requirements,
5. ask for baseline confirmation when needed,
6. initialize `.ai-team/` and `docs/harness/`,
7. generate the project team architecture,
8. dispatch implementation and review work,
9. verify delivery,
10. prepare code delivery evidence,
11. run a retrospective.

It should decide which collaboration tools are useful from context. Local harness files are the fallback. It should stop at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are outside this harness.

See `examples/full-project-flow.md` for a full request-to-delivery walkthrough.

## Narrow Tasks

Use smaller skills when you do not need the whole project operating system:

```text
$project-bootstrap
检查并初始化当前项目的 git、.ai-team、docs/harness，以及需要使用的 GitHub/Linear/Notion/Figma/Slack 映射。
```

```text
$project-runtime
更新项目阶段、任务、决策、QA 和交付记录，并校验本地 harness 状态。
```

```text
$requirement-baseline
帮我把这个需求问清楚，形成可验收的需求基线。
```

```text
$minimal-safe-change
用最小改动完成这个字段兼容，不要做无关重构。
```

```text
$independent-quality-gate
独立验收当前实现，重点检查 API 返回、前端类型和数据库字段是否一致。
```

```text
$delivery-readiness
整理本次代码交付证据，包括验收映射、变更文件、测试结果、QA 结论和遗留风险。
```

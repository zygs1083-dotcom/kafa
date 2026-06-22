# Quick Start

## Full Project

Say:

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

The `project-harness` skill should:

1. inspect the workspace,
2. clarify requirements,
3. ask for baseline confirmation when needed,
4. initialize `.ai-team/` and `docs/harness/`,
5. generate the project team architecture,
6. dispatch implementation and review work,
7. verify delivery,
8. prepare release evidence,
9. run a retrospective.

## Narrow Tasks

Use smaller skills when you do not need the whole project operating system:

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

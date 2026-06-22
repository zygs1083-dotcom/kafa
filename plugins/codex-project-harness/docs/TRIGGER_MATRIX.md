# Trigger Matrix

| User intent | Preferred skill | Notes |
| --- | --- | --- |
| 我要开发/创建/搭建一个项目 | `project-harness` | Full lifecycle orchestration |
| 帮我把需求问清楚 | `requirement-baseline` | Use before implementation |
| 设计 Agent 小队/项目团队 | `team-architecture` | Creates role and skill plan |
| 最小改动/不要重构 | `minimal-safe-change` | Keep diff narrow but complete |
| 先写测试/测试驱动 | `test-first-delivery` | Contract and regression oriented |
| 修 bug/复现问题 | `bug-fix-loop` | Reproduction before fix |
| 独立验收/代码审查/QA | `independent-quality-gate` | Reviewer must not be the producer |
| 上线检查/发布准备 | `release-readiness` | Requires approval for production |
| 检查方法论漂移/Agent 漂移 | `harness-audit` | Maintenance and drift repair |
| 复盘/沉淀方法论 | `project-retrospective` | Convert evidence into improvements |

## Should Not Trigger

| Request | Reason |
| --- | --- |
| 解释什么是上线 | Educational, not release execution |
| 什么是 Agent | Conceptual explanation |
| 翻译这段话 | Translation task |
| 总结我提供的文本 | Summarization unless it asks for project artifacts |

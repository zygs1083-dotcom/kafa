# Trigger Matrix

| User intent | Preferred skill | Notes |
| --- | --- | --- |
| 我要开发/创建/搭建一个项目 | `project-harness` | Code delivery orchestration |
| 初始化项目/检查协作工具/配置 GitHub Linear Notion Figma Slack | `project-bootstrap` | Workspace, git, harness, and collaboration tool mapping |
| 更新阶段/任务/QA/交付记录/检查本地状态 | `project-runtime` | Executable local control plane |
| 帮我把需求问清楚 | `requirement-baseline` | Use before implementation |
| 设计 Agent 小队/项目团队 | `team-architecture` | Creates role and skill plan |
| 最小改动/不要重构 | `minimal-safe-change` | Keep diff narrow but complete |
| 先写测试/测试驱动 | `test-first-delivery` | Contract and regression oriented |
| 修 bug/复现问题 | `bug-fix-loop` | Reproduction before fix |
| 独立验收/代码审查/QA | `independent-quality-gate` | Reviewer must not be the producer |
| 交付检查/验收交付 | `delivery-readiness` | Package verified code handoff evidence |
| 检查方法论漂移/Agent 漂移 | `harness-audit` | Maintenance and drift repair |
| 复盘/沉淀方法论 | `project-retrospective` | Convert evidence into improvements |

## Should Not Trigger

| Request | Reason |
| --- | --- |
| 帮我部署/上线/发布到生产 | Deployment and production release are outside this harness |
| 只发一条 Slack 消息 | Use Slack workflow directly unless tied to project delivery |
| 只改一个 Figma 文件 | Use Figma workflow directly unless tied to project delivery |
| 解释什么是上线 | Educational, not code delivery execution |
| 什么是 Agent | Conceptual explanation |
| 翻译这段话 | Translation task |
| 总结我提供的文本 | Summarization unless it asks for project artifacts |

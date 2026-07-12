# Trigger Matrix

| User intent | Preferred skill | Notes |
| --- | --- | --- |
| 我要开发/创建/搭建项目，或需要完整验证交付 | `project-harness` | Single entrypoint for bootstrap, OpenSpec routing, local Kernel facts, QA, and verified handoff |
| 最小改动/不要重构 | `minimal-safe-change` | Keep diff narrow but complete |
| 先写测试/测试驱动 | `test-first-delivery` | Contract and regression oriented |
| 修 bug/复现问题 | `bug-fix-loop` | Reproduction before fix |
| 独立验收/代码审查/QA | `independent-quality-gate` | Reviewer must not be the producer |
| 检查方法论漂移/Agent 漂移 | `harness-audit` | Maintenance and drift repair |
| 复盘/沉淀方法论 | `project-retrospective` | Convert evidence into improvements |

Broad, ambiguous, architectural, cross-module, or long-lived work routes from
`project-harness` to OpenSpec before implementation. The retained seven Skills
do not own a Host task lifecycle; Native Codex/ChatGPT owns tasks, subagents,
worktrees, approvals, models, cancellation, steering, and handoff.

## Should Not Trigger

| Request | Reason |
| --- | --- |
| 帮我部署/上线/发布到生产 | Deployment and production release are outside this harness |
| 只执行一个外部 SaaS 动作 | External Apps/tools are Host-owned and outside the local Kafa runtime |
| 解释什么是上线 | Educational, not code delivery execution |
| 什么是 Agent | Conceptual explanation |
| 翻译这段话 | Translation task |
| 总结我提供的文本 | Summarization unless it asks for project artifacts |

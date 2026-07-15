# Goal Mode Prompt

请在 `/Users/wangying/AI-Codex/personal/方法论/kafa` 开启目标模式并创建一个持续目标，目标是：

> 严格实施 OpenSpec change `local-core-slimming`，把 Kafa 从包含外部 Connector 和重复 Host 编排的控制面，收缩为 local-only verified delivery kernel；完成 schema 30 可回滚迁移、外部与 legacy surface 删除、single-writer task lifecycle、immutable execution/validation 模型、Skill/Hook/CLI/schema 收缩、local-only evals、完整回归和最终审计。

执行约束：

1. 首先读取仓库适用的 `AGENTS.md`，然后依次读取：
   - `openspec/changes/local-core-slimming/proposal.md`
   - `openspec/changes/local-core-slimming/design.md`
   - `openspec/changes/local-core-slimming/specs/local-delivery-kernel/spec.md`
   - `openspec/changes/local-core-slimming/tasks.md`
2. 运行 `openspec status --change local-core-slimming` 和
   `openspec validate local-core-slimming`，确认计划完整后再改代码。
3. 以 `tasks.md` 为唯一实施清单，严格按依赖顺序推进；每完成一项立即将对应
   checkbox 改为 `[x]`，不得在最后一次性全勾。
4. 开始前确认 `main`、`origin/main` 和工作区状态，创建分支
   `v2-local-core-slimming`。遇到不是本目标产生的改动时保留并与之协作，不得
   reset、checkout 或覆盖用户改动。
5. 不重新讨论已经在 design 中锁定的决策。只有出现会导致数据丢失、无法迁移、
   与真实 Codex host contract 冲突或无法通过 spec 的阻塞事实时，才暂停并向用户
   报告证据。
6. 先建立红测和 migration backup/rollback，再删除 production surface。不得先
   大规模删除后补测试。
7. Native Codex/ChatGPT 是 task、thread、subagent、worktree、approval、model、
   cancel、steer、handoff 的唯一 owner；Kafa 不再实现第二套 lifecycle。
8. 业务 runtime 必须 local-only。不得新增或保留 GitHub、Linear、Notion、Figma、
   Slack 直连、Connector token、`gh api`、legacy Host SDK worker 或伪造 receipt。
   本地 Git 和维护 Kafa 自身的 GitHub CI/release workflow 继续保留。
9. Delivery trust 不得放宽。manual 文本、self-reported session id、同进程 HMAC、
   raw worker output、Hook output 和 eval output 都不能冒充 controller execution。
   High/critical 无可验证外部/host provenance 时必须返回
   `human-review-required`，除非用户明确接受并完整记录风险。
10. 只允许把已锁定、低风险、1-3 个文件、有确定测试或 inventory 验收的机械任务
    交给 Spark 子 agent。schema、migration、trust、delivery gate、数据模型和跨模块
    整合必须由主模型负责；主模型必须复验所有子 agent diff 和测试。
11. 每个 Wave 结束都运行该 Wave 的 targeted tests 和 checkpoint；不能因为完整回归
    较慢而跳过。长命令必须等待结束，不得留下后台会话。
12. 经常汇报进度，但以事实、测试和剩余风险为主。不要把 skipped、blocked、
    not-run 或 fixture-only 结果描述为通过。
13. 最终必须完成 `tasks.md` 第 11 组全部验证、对抗式审查、before/after metrics 和
    OpenSpec validation。
14. 未经用户明确要求，不 commit、push、merge、tag、release 或 deploy。完成实现和
    验证后停在清晰 handoff，报告 git 状态和下一步建议。

目标完成条件：`tasks.md` 全部完成，所有 spec scenarios 有验证证据，schema/CLI/
Skill/Hook/性能预算达到或有用户批准的明确偏差，完整回归通过，工作区只包含计划内
改动，并且没有未说明的残余外部 Connector 或 legacy Host runtime surface。

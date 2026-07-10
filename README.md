# Codex Project Harness

Codex Project Harness 是一套面向 Codex 的通用代码交付方法论与本地运行时插件。它把一次“我要开发一个需求”的对话，组织成可追踪、可验证、可交付的工程流程：先澄清需求，再建立验收标准和失败模式，随后拆分任务、实现代码、执行测试、独立 QA，最后产出代码交付证据。

这个项目不是某个业务系统的模板，也不是只适用于某个技术栈的脚手架。它是一个通用能力层，可以用于前端、后端、全栈、数据、自动化、插件、CLI、文档型工程等不同项目。外部协作工具可用时会被纳入流程，不可用时仍然能依赖本地 `.ai-team/` 和 `docs/harness/` 文件完成交付。

当前源码候选版本是 **v1.25.0-beta.1**，`release.json` 将其明确标记为 `development`，因此它不是已发布版本。最新正式 tag/release 以 GitHub 和 `git tag` 为准。当前架构代际定位为 **Codex Harness Kernel v4.18.0**。它只负责交付经过验证的代码和证据，不负责生产部署、上线发布、基础设施开通、生产迁移、密钥变更或付费资源创建。

## 版本与发布

本项目从 `v0.4.0-beta.1` 开始使用正式 Git tag 标记版本。普通 commit 和 `release_state=development` 只表示开发源码；只有 `release.json`、版本文件、Changelog、tag 和 GitHub prerelease 全部一致时，版本才是可回看、可安装、可对比的发布点。

```bash
cat VERSION
cat release.json
python3 -m kafa.release --json
git tag --list
git show v0.4.0-beta.1
git log <old-tag>..<new-tag> --oneline
```

版本变化记录见 [CHANGELOG.md](CHANGELOG.md)。Tag workflow 会先执行 `python -m kafa.release --require-tag`、完整回归和真实隔离安装 smoke，再构建 wheel 与完整 source archive 并创建 GitHub prerelease；开发状态下即使误推 tag 也会 fail-closed。

## 这个项目解决什么问题

很多 AI 编程协作会停留在“直接写代码”的层面，容易出现几个问题：

- 需求没有确认清楚，代码写完才发现范围错了。
- 任务拆分和执行记录只存在聊天上下文里，换会话后丢失。
- 测试和 QA 是事后补充，缺少验收标准和失败场景映射。
- 多 agent 协作只有角色名称，没有明确的状态、证据和边界。
- GitHub、Linear、Notion、Figma、Slack 等工具没有统一进入工程流。
- 最终交付只给一段总结，缺少可审计的变更、测试、风险和质量门记录。

Codex Project Harness 的目标是把这些隐性流程显式化、结构化、可执行化。

## 工作方法约束

Harness 的默认工作方式不是“直接动手写代码”，而是先把问题压回到可验证的工程单元：

- 动手前先回到根本问题：本任务到底要解决什么问题，避免照搬惯例或默认流程。
- 把问题拆到最小、能验证的单元，逐个实现和复验。
- 每个关键决定都要说得出“为什么”，而不只是“怎么做”。
- 交付前切换成最挑剔的审查者，从逻辑漏洞、事实错误、更简单方案、可验证证据四个角度攻击自己的方案。
- 不能只说“看起来没问题”；必须给出验证证据，或者明确写出剩余风险和未验证范围。

这些约束不替代 Kernel 的 delivery gate，也不让主观判断变成可信 evidence。它们是 Skill、Agent 模板和人工协作共同遵守的工作纪律。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 需求基线 | 将模糊想法转成目标、用户场景、功能范围、非目标、约束和验收标准 |
| 追踪链 | 结构化维护需求 → 验收 → 任务 → 验证 → 交付的链路 |
| 项目启动检查 | 检查工作区、Git、分支、远端、项目说明和本地 harness 文件 |
| 运行时状态 | 用脚本维护阶段、任务、决策、验证、质量门和交付记录 |
| Agent 小队方法 | 用项目经理、产品、架构、开发、QA、交付等角色组织协作 |
| 子 agent 执行 | 将明确的独立任务拆给短生命周期子 agent，并要求返回证据 |
| Failure Mode Engineering | 为风险场景建立失败模式矩阵，推动测试覆盖和恢复策略 |
| Test-first Delivery | 鼓励先定义可执行验证，再实现最小安全改动 |
| Independent Quality Gate | 在交付前记录独立 QA 结论、reviewer context、阻塞问题和剩余风险 |
| 协作工具适配 | Git/GitHub、Linear、Notion、Figma、Slack 可按上下文进入流程 |
| Connector 韧性兜底 | 外部 connector 限流/失败会记录 budget、blocked finding 和本地事实源 fallback，不重复外部写入 |
| Connector 命名空间隔离 | 每个项目绑定已有外部目标，写入前强校验 project profile，并用 project-key + idempotency-key 双 marker 防止跨项目污染 |
| AgentRunner 与并行隔离 | dispatch 可显式使用本地子进程 runner，并通过 git worktree 与文件 claim 隔离并行编辑 |
| 原生 Codex 子 agent | 安装 `.codex/agents/*.toml`，为 `spawn_agents_on_csv` 导出/导入原生 CSV 与 schema，并由控制器重新验证 worker 报告 |
| AgentProvider 生命周期 | 可审计管理 host/manual/fixture provider session 的 start、collect、cancel、reconcile；Host Codex start 只登记并启动后台 worker，不等待完整 turn；provider report 仍只是 raw report |
| 原生任务路由建议 | `dispatch route-advice` 只输出风险与可复验能力提示；具体模型、推理、沙箱、审批和任务生命周期由 ChatGPT/Codex 宿主决定 |
| Session Attestation | 用 `agent_sessions` 与 HMAC `session_attestations` 证明独立 QA 来自不同会话/上下文，高风险 QA 不能只靠角色字符串 |
| 真实沙箱复验与集成硬化 | `dispatch verify-attempt --runner container` 通过 Docker/Podman no-network 容器复验；`dispatch integrate` 只合并已验证、未漂移且文件 claim 覆盖的分支 |
| Agent E2E 评测 | `run_agent_e2e_eval.py` 提供 fixture、stability、live-codex 三层评测，验证调度、provider raw report、controller verify、file claim、integration 阻断和稳定性矩阵 |
| 安装和发行 | `kafa` console script 生成 Codex marketplace 安装入口，并提供 install/upgrade/uninstall/doctor |
| Codex Hooks 护栏 | 插件自带 `SessionStart`、`SubagentStart`、`PreToolUse`、`PostToolUse`、`Stop` hooks，用于状态注入、边界提醒和 readiness 检查 |
| 架构控制面 | Skill 是自然语言入口，Plugin 负责分发，Hooks/Host/Connector/Eval 都不能绕过 Kernel 事实源与门禁 |
| 持续迭代 Cycle | `cycle start/status/close` 将长期项目拆成当前 delivery cycle；旧记录保留审计价值但不永久阻断新 candidate |
| 代码交付边界 | 明确停止在 verified code handoff，不自动进入部署或生产操作 |

## 架构概览

Codex Project Harness 使用三层执行模型。

```text
Layer 0: Project Manager
  - 当前会话的总控
  - 维护需求、状态、决策和交付边界
  - 判断是否需要工具、角色和子任务

Layer 1: Domain Sessions
  - Product / Architecture / Development / QA / Security / Delivery
  - 按领域保留上下文
  - 输出结构化任务、判断和证据

Layer 2: Subagents
  - 短生命周期任务执行单元
  - 可用于实现、测试、审查、风险排查
  - 不一定拥有独立用户会话，但必须返回可验证产物
```

一个典型请求会沿着下面的路径流动：

```text
User Request
  -> Project Bootstrap
  -> Requirement Baseline
  -> Confirmation Gate
  -> Team Architecture
  -> Planning
  -> Implementation
  -> Test / Validation
  -> Independent Quality Gate
  -> Delivery Readiness
  -> Retrospective
```

## 工作流程

当用户对 Codex 说：

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

Harness 应该按以下方式工作：

1. 读取当前项目和仓库状态。
2. 初始化或修复 `.ai-team/` 和 `docs/harness/` 本地控制面。
3. 检查 Git/GitHub/Linear/Notion/Figma/Slack 是否有用。
4. 形成需求基线：目标、用户、场景、功能、非目标、约束。
5. 建立验收标准，例如 `AC1`、`AC2`、`AC3`。
6. 为风险行为建立失败模式，例如 `FM1`、`FM2`。
7. 让用户确认范围，或在低风险清晰任务中记录假设后继续。
8. 选择最小有效 agent 小队。
9. 将需求拆成任务，例如 `T1`、`T2`、`T3`。
10. 让 producer 负责实现，让 reviewer 或 QA 做独立检查。
11. 执行测试、lint、build、手工检查或其它项目适配验证。
12. 记录质量门：reviewed commit、base/head commit、source tree hash、tracked diff hash、reviewer context、result、blocking findings。
13. 产出交付说明：变更内容、验收映射、测试证据、遗留风险和外部链接。

完整示例见 [examples/full-project-flow.md](examples/full-project-flow.md)。

## 本地运行时控制面

Harness 会在目标项目中维护一个结构化事实源，并生成两类 Markdown 视图。

结构化事实源：

```text
.ai-team/state/harness.db
```

这是 SQLite 数据库，保存 project、requirements、acceptance、failure modes、tasks、validations、evidence、tests、findings、invalidations、quality gates、deliveries、adapter mappings、agents、migrations 和 events。SQLite 使用事务、WAL、外键、唯一约束和 task revision/lease/heartbeat 来支持多会话和多 agent 协作。

从 v0.7 开始，运行时引入 **Kernel v3** 一致性内核。CLI 和 legacy wrappers 会经过 `core.api`，写入路径统一经过 schema guard、调度/锁/门禁、事务、event bus、invariant checker 和 projections。SQLite 状态表仍是主事实源；event bus 用于审计和校验，可信恢复路径是 checkpoint snapshot export/import。

从 v1.0 开始，交付门禁只接受执行器真实运行并解析出的语义可信证据：passing validation 必须引用 gateable test target，命令必须匹配目标模板，退出码必须为 `0`，`executed_count_source` 必须为 `parsed`，`executed_count` 必须大于 `0`，并保留 stdout SHA-256、artifact path、当前 source tree hash 和 trust anchor。旧自由文本或手填命令证据仍可审计记录，但不具备交付资格。

从 v1.0.1 开始，门禁进一步 fail-closed：无 git 项目不会静默跳过代码身份校验，必须显式记录 `--code-identity content-hash`；用于交付的证据必须有非空且当前有效的 source hash；stdout artifact 会在门禁阶段重算 SHA-256；`ci` 与 `external-session` 高信任锚必须来自 connector-origin 契约，manual-origin 记录只作为审计事实。

从 v1.0.2 开始，connector-origin 不再只看 `verification_token` 是否非空，而是要求宿主保管的 HMAC key 参与校验。运行时从 `HARNESS_CONNECTOR_KEY` 或 `.ai-team/control/connector-key-path.txt` 指向的文件加载 key，并用该 key 计算 CI / external-session verification token。没有 key、token 不匹配、commit SHA 或 conclusion 被篡改时，该记录在门禁中降级为 manual/local-only 等价，不能覆盖 high/critical failure mode。key 本身不得写入 DB、事件、Markdown 或 Git；推荐放在已忽略的 `.ai-team/runtime/connector.key`。

从 v1.1.0 开始，任务 lease 使用 fencing 防止过期持有者覆写新持有者工作。`task claim` 和 `task review` 会输出 `fence=<n>`；`task start|heartbeat|submit|complete|accept|block|release` 可传 `--fence <n>`，当任务已被回收或重新交接导致 fence 过期时，写回会以 `fence-stale` 在事务内失败并回滚。

从 v1.1.1 开始，多数写命令支持 `--request-id` 命令幂等。首次执行会在业务事务内写入 `command_log`，重试同一 request id 与相同参数时直接返回首次 stdout，不重复应用业务变更；相同 request id 搭配不同参数会返回 `idempotency-conflict`。`init`、`migrate`、`repair`、`checkpoint create/import` 暂不支持 `--request-id`。

从 v1.6.0 开始，独立 QA 不再只依赖 `developer` / `qa-reviewer` 这样的字符串角色。`session attest` 会记录 `agent_sessions` 和 `session_attestations`；connector-origin session attestation 使用宿主保管的 HMAC key 校验 `agent-session:{session_id}:{agent_id}:{role}:{context_id}`。`task submit --session-id` 会保存 producer session，`task review/accept --session-id` 会要求 reviewer session 活跃且与 producer session 不同。旧式不传 session 的流程仍兼容，但只具备 `local-only` 语义。高/critical 风险的最终 delivery gate 需要 connector(HMAC) reviewer session attestation；manual/local session 只能作为审计或 low/medium 风险路径。

从 v1.7.0 开始，控制器复验可以显式使用真实容器执行：`dispatch verify-attempt --runner container [--container-image <image>]` 会在 agent branch 的只读代码副本中用 Docker/Podman 运行目标命令，默认断网、最小资源限制，不挂载宿主 HOME/SSH/Git 凭证，并把 stdout/stderr 由宿主写入 artifact。Docker/Podman 不可用时，container runner 以 `sandbox-unavailable` fail-closed，不会静默降级为 local。`dispatch integrate` 也会在 merge 前强制检查：每个 agent 分支必须有最新 `task_attempt.status=verified`，当前 head/tree 必须匹配复验证据，且 `git diff base..branch` 只能包含该 task/agent 的 active file claims。

从 v1.8.0 开始，仓库新增真实 Agent E2E 评测脚本。`run_agent_e2e_eval.py --mode fixture` 会在临时 Git repo 中调用真实 CLI，覆盖并行成功、依赖阻塞、同文件 claim 冲突、伪造 worker evidence 阻断、集成后回归阻断五个场景，并输出稳定 JSON 指标。`run_skill_eval.py` 仍保留为 transcript marker 检查，但不再代表 Agent 能力评测。

从 v1.12.0 开始，Agent E2E 升级为稳定性矩阵 runner。`--mode stability` 是 CI 发布闸，包含 fixture 五场景、fake Host Codex SDK、三角色 session lifecycle、connector mock server、crash/retry recovery 和 SQLite contention stress；`--mode live-codex` 是 opt-in 宿主真实 Codex profile，只有设置 `HARNESS_E2E_ENABLE_LIVE_CODEX=1` 且本机 Codex 可用时才进入真实 live 路径。未启用 live 时会明确输出 `live_skipped=true` 和 skip reason，这不代表真实 Codex E2E 通过。

从 v1.13.0 开始，仓库新增根级 `kafa` 安装发行助手。`python3 -m pip install -e .` 会安装 stdlib-only 的 `kafa` console script；`kafa plugin install|upgrade|uninstall` 管理 Codex 官方 marketplace JSON，`kafa doctor` 做 Python/Git/manifest/structure 预检。只有使用 legacy Host Codex Provider 时才需要 `python3 -m pip install -e '.[host-codex]'`。该助手只管理本地安装入口，不发布 PyPI，不直接改 Codex cache，也不替代 runtime `harness.py`。

从 v1.14.0 开始，项目将 Skill、Plugin、Hooks、Host Bridge、Kernel、Connectors、Evals 收束为可验证的 Architecture Control Plane。详见 [docs/runtime/CONTROL_PLANE.md](docs/runtime/CONTROL_PLANE.md)。`kafa doctor --repo .` 会检查 control-plane contract：Skill 只是自然语言入口，Hooks 是 advisory，Host/Connector 只产生 raw/audit 记录，可信交付仍由 Kernel controller verification、HMAC/session attestation、integration/delivery gate 决定。

从 v1.15.0 开始，真实 connector adapter 增加韧性和兜底治理。GitHub、Linear、Notion、Figma、Slack 的 probe/write 都会记录 `connector_budgets`，处理 `Retry-After`、429/529 和常见 rate limit 信号；超过 retry budget 的 action 会标记 `blocked` 并写 finding，但本地 `.ai-team/` 事实源仍可继续支持交付流程。写入前会按 idempotency marker 尝试复用已有外部对象，降低外部成功但本地事务未提交后的重复写风险。Connector 结果仍只是 workflow sync，不是 delivery-eligible evidence。

从 v1.16.0 开始，blocked connector 会自动进入 Advisory Fallback Layer：Harness 在本地生成 `docs/harness/advisory-fallbacks/<action-id>.md` 和 `.ai-team/control/advisory-fallbacks.md`，给出 GitHub 草稿、Linear 任务兜底、Notion 文档草稿、Product Design brief/visual QA checklist、Slack handoff summary 等二级替代分析。它只辅助人和 agent 继续推进，不调用真实官方插件，不冒充外部写入，也永远不能满足 delivery evidence。

从 v1.19.0 开始，Kernel 支持持续迭代的 Delivery Cycle 模型。`cycle status --json` 查看当前 cycle；`cycle close --status delivered|archived` 关闭当前 cycle；`cycle start --id <cycle-id> --name <name> --goal <goal>` 开启下一轮。Delivery gate 只检查当前 cycle、当前 candidate 的 active validation、quality gate、invalidation、task 和 high/critical failure-mode coverage。旧 cycle 的失败验证、旧 quality gate 和 stale source hash 保留审计价值，但不会永久阻断新 cycle；新 cycle 也不会自动继承旧证据，必须重新建立当前 candidate 的可信验证。

从 v1.20.0 开始，Connector 写操作使用 Transactional Outbox。`adapter confirm` 会先用短事务和 `execution_fence` claim action，再在事务外执行远程写入，最后用同一 fence CAS 写回 completed；并发 confirm 未抢到 claim 时不会调用远程 API。若写入结果未知，action 会进入 `unknown`，后续 confirm/reconcile 必须先按 idempotency marker 远程恢复，无法确认时 fail-closed。Connector 结果仍只是 workflow sync，不是 delivery-eligible evidence。

从 v1.21.0 开始，Kernel 增加 P1 Reliability Hardening：dispatch run 状态由所有 assignment 聚合，不再被单个任务提前覆盖；`test-target add` 可以记录 `--stack-profile`、`--container-image`、`--requires-sandbox`、`--requires-no-network`、`--result-format` 和 `--result-path`；container verification 使用只读 `/src` 加可写 `/workspace` 的 no-network 沙箱；JUnit、pytest JSON、Jest JSON、go test JSON、cargo nextest JSON 和 Playwright JSON 可作为结构化测试语义证据。需要 sandbox/no-network 的 target 不能通过 local runner 生成可信 pass validation；结构化证据不放宽 HMAC/session attestation、integration 或 delivery gate。

从 v1.22.0 开始，Connector Namespace Isolation 成为 Kernel 事实。每个项目会有 `connector_project_key`，真实 connector 写入前必须先通过 `connector profile set` 绑定已有 GitHub repo、Linear team/project、Notion parent page、Slack channel 或 Figma file。Harness 不自动创建外部 workspace、project、channel、file 或 repo；无 profile 或 scope mismatch 时，`execute=true` 写入 fail-closed，不调用外部 API。外部正文 marker 升级为 `project-key` + `idempotency-key` 双 marker，marker recovery、budget 和 advisory fallback 都按项目隔离。

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile set \
  --project-key my-project \
  --github-repo owner/repo \
  --linear-team TEAM \
  --notion-parent PAGE_ID \
  --slack-channel C123456 \
  --figma-file FILE_KEY
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile status --json
```

原生 ChatGPT/Codex 路径不由 Kafa 选择模型。`dispatch route-advice` 只输出任务风险和 `small_verified_candidate` 能力提示，宿主拥有具体模型、推理、沙箱、审批以及 task/thread/worktree 生命周期。旧 Host Codex Provider 仍保留显式兼容策略：`HARNESS_CODEX_MODEL` 是最高优先级硬覆盖；没有该覆盖时，`HARNESS_CODEX_MODEL_POLICY=spark-deterministic` 必须同时显式设置 `HARNESS_CODEX_SPARK_MODEL=<model>`，且只有满足低风险可复验规则的任务才会选该模型。Kafa 不内置 preview 模型名，模型选择也不是可信 evidence。

从 v1.24.0 开始，冷启动路径分成两类诊断：`kafa doctor --repo <kafa-repo>` 只检查 Kafa/插件源码仓库和 marketplace 安装；`kafa project doctor --repo <business-project>` 检查普通业务项目是否已初始化 Harness，不再要求业务项目包含 `plugins/codex-project-harness/`。项目内可用 `harness.py --root . quickstart status` 查看缺失清单，或用 `quickstart minimal --execute` 在一个已有测试命令上跑通 requirement、acceptance、task、dispatch evidence、validation、quality gate 和 delivery 的最小闭环。

从 v1.25.0 开始，执行前可以运行 `dispatch route-advice [--run-id] [--json]`。它只读分析 ready tasks、test target、failure mode risk 和 agent role，输出 `native-host-small-verified`、`native-host-general`、`main-model-or-manual` 或 `blocked-not-ready`。带 run id 时下一步是 `dispatch native-export`；它不会启动 provider、选择模型或生成 evidence。

从 v1.8.1 开始，仓库进入 Phase 0 功能扩张冻结。该维护版不新增 schema、命令、Skills、状态或运行时抽象，而是通过结构验证和 `tests/test_feature_freeze.py` 固定 runtime surface。v1.15 显式把 schema baseline 提升到 23，允许 connector budget 表、adapter action retry/block 字段和对应 schema 文件；v1.16 显式把 schema baseline 提升到 24，允许 `advisory_fallbacks` 表和对应 schema 文件。v1.17 修复 Host Codex Provider 非阻塞生命周期；v1.18 在 schema 24 内把 Host Codex 执行固定到独立 git worktree 并迁移到 mandatory Codex SDK；v1.19 显式把 schema baseline 提升到 25 并允许 `cycle` CLI surface，用于修复一次性交付模型；v1.20 显式把 schema baseline 提升到 26，用于 connector outbox fence、claim lease 和 unknown recovery 审计；v1.21 显式把 schema baseline 提升到 27，用于 target sandbox policy、stack profile 和 structured test semantic evidence；v1.22 显式把 schema baseline 提升到 28 并允许 `connector profile` CLI surface，用于项目级外部空间边界；v1.23 只增加 legacy Host Codex 内部模型策略和审计元数据；v1.24 在 schema 28 内增加 cold-start guided loop 的 `quickstart` 和 `task accept-ready` 引导入口；v1.25 在 schema 28 内增加只读 `dispatch route-advice`，当前已收束为原生宿主的风险与能力提示。当前 stop-ship correctness 工作把存储基线迁移到 schema 29，为 cycle-local identity、gate sequence 和 legacy trust downgrade 提供可回滚契约；这不是发布声明。Skill/core/script/hook 文件集合仍保持冻结。后续若继续扩张 runtime surface，必须在对应 PR 中显式更新冻结基线并解释原因。

从 v1.11.0 开始，插件自带 Codex lifecycle hooks。安装或更新插件后，用 `/hooks` 审核并信任它们；也可以用 `[features] hooks = false` 关闭 Codex hooks。默认 hooks 只做辅助护栏：`SessionStart` 注入项目状态，`SubagentStart` 提醒角色/任务/验收边界，`PreToolUse` 在需求未确认或无 active task 时提示写入风险，`PostToolUse` 汇总变更，`Stop` 运行 readiness 检查。若插件不在项目默认 `plugins/codex-project-harness` 路径下，设置 `CODEX_PROJECT_HARNESS_PLUGIN_ROOT`。Hooks 不生成可信 evidence，也不能替代 controller verification、integration gate、HMAC/session attestation 或 CI。

从 v1.18.0 开始，现有 `dispatch provider start --provider host-codex` 是非阻塞、worktree-isolated 的 legacy Host Codex SDK 入口。该 provider 需要可选 extra `kafa[host-codex]`，且不能继承当前原生任务的完整权限模型，因此不是新任务的默认编排路径，并默认 fail-closed；只有显式设置 `HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all` 才接受固定的 sandbox/approval 兼容边界。独立 watchdog 执行真实 turn deadline，cancel 尽力终止已知 worker process tree，collect 不再给死亡 worker 续租。由于 detached/reparent helper 无法由 legacy bridge 独立证明已终止，cancel/timeout 一律保持 `verification_failed` 且不自动 replan；可信生命周期确认属于原生宿主。最终 JSON 仍只作为 raw provider report，交付资格仍必须由 controller 复验生成。

从 v1.10.0 开始，现有 `adapter confirm` 在 `adapter_actions.payload_json` 含 `{"execute": true, "operation": "...", "params": {...}}` 时可以执行真实 connector adapter。GitHub 通过 `gh api` 执行；Linear、Notion、Figma、Slack 通过官方 HTTP API 和环境变量 token 执行。外部写入结果只进入 adapter/action 记录，不自动成为 delivery evidence，也不放宽 high/critical 的 HMAC 信任要求。

长期 Connector 边界已在 [Apps/MCP Connector Receipt ADR](docs/runtime/APPS_MCP_RECEIPT_ADR.md) 中锁定：ChatGPT Apps/MCP 负责授权、workspace policy、tool approval 和外部动作，Kafa 只治理 project scope、immutable intent、outbox fence、receipt validation 与 fallback。当前 `gh`/HTTP 路径明确属于 `legacy-direct` 兼容模式；ADR 不代表 receipt runtime 已实现，也不会把 Apps/MCP 工具输出升级为 delivery evidence。

原生 Agent 生命周期边界见 [Native Codex Runtime ADR](docs/runtime/NATIVE_CODEX_RUNTIME_ADR.md)：Codex/ChatGPT 宿主管理 task、thread、subagent、worktree、approval、model、steer、cancel 与 handoff；Kafa 根控制器只导出不可变 task package、导入真实 host receipt，并继续负责 controller verification 和 delivery gate。当前 SQLite 主事实源保持 root-workspace single-writer，managed worktree 不复制或修改数据库；hosted transport 未实现前不冒充兼容。

信任等级按强度分为三档：

- `local-only`：本地模型会话执行证据，可覆盖 low/medium 风险。
- `human-confirmed`：人工确认记录，可覆盖 low/medium 风险，但不能冒充外部执行。
- `connector(HMAC)`：宿主/connector 用模型会话拿不到的 key 生成或验证 HMAC，可覆盖 high/critical 风险。

Markdown 文件是面向人的派生视图。

`.ai-team/` 用于项目控制、需求和计划：

```text
.ai-team/
  control/
    capability-report.md
    project-charter.md
    project-state.yaml
    agent-registry.md
    tooling-map.md
    risk-register.md
    decision-log.md
  requirements/
    requirements.md
    acceptance.md
    failure-modes.md
    traceability.md
  planning/
    roadmap.md
    task-board.md
```

`docs/harness/` 用于交付证据和过程文档：

```text
docs/harness/
  bootstrap.md
  team-architecture.md
  workflow.md
  runtime.md
  design-context.md
  validation.md
  quality-gates.md
  delivery.md
  evolution-log.md
```

运行时状态和事件的事实源是：

```text
.ai-team/state/harness.db
```

其中 `events` 表保存可审计事件流；`.ai-team/` 和 `docs/harness/` 下的 Markdown 文件由运行时渲染生成，适合阅读和交付，但不作为唯一事实源。

## 插件目录结构

仓库的主要内容在 `plugins/codex-project-harness/`。

```text
plugins/codex-project-harness/
  .codex-plugin/
    plugin.json
  skills/
    project-harness/
    project-bootstrap/
    project-runtime/
    requirement-baseline/
    team-architecture/
    minimal-safe-change/
    test-first-delivery/
    bug-fix-loop/
    independent-quality-gate/
    delivery-readiness/
    harness-audit/
    project-retrospective/
    project-runtime/scripts/harness.py
  scripts/
    harness.py
    harness_db.py
    harness_wrapper.py
    init_project_harness.py
    validate_structure.py
    harness_status.py
    update_phase.py
    add_acceptance.py
    add_failure_mode.py
    add_task.py
    update_task.py
    record_decision.py
    record_validation.py
    record_quality_gate.py
    record_delivery.py
    validate_harness_state.py
  hooks/
    hooks.json
    harness_hook.py
  references/
    collaboration-tools.md
    tool-adapters.md
  schemas/
    project-state.schema.json
    requirement.schema.json
    acceptance.schema.json
    task.schema.json
    event.schema.json
    failure-mode.schema.json
    quality-gate.schema.json
    validation.schema.json
    evidence.schema.json
    test.schema.json
    finding.schema.json
    invalidation.schema.json
    delivery.schema.json
    adapter.schema.json
    agent.schema.json
  templates/
    agents/
    project/
```

## Skills

| Skill | 使用场景 |
| --- | --- |
| `project-harness` | 完整项目或功能的总控入口，从需求到代码交付 |
| `project-bootstrap` | 检查工作区、Git、分支、本地控制面和协作工具映射 |
| `project-runtime` | 更新阶段、任务、验收、失败模式、验证、质量门和交付记录 |
| `requirement-baseline` | 澄清需求并形成可确认、可验收的范围 |
| `team-architecture` | 为任务选择最小有效 agent 小队和协作模式 |
| `minimal-safe-change` | 小范围、安全、低风险改动 |
| `test-first-delivery` | 测试优先、契约敏感或需要回归覆盖的实现 |
| `bug-fix-loop` | 复现、定位、修复和验证 bug |
| `independent-quality-gate` | 交付前独立 QA、代码审查和集成一致性检查 |
| `delivery-readiness` | 汇总代码交付证据和遗留风险 |
| `harness-audit` | 审计 harness 文件、状态和流程漂移 |
| `project-retrospective` | 复盘项目过程并沉淀方法论改进 |

## 运行时脚本

这些脚本让方法论不只停留在 Markdown 文档里。

推荐使用统一 CLI：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . init
python3 plugins/codex-project-harness/scripts/harness.py --root . doctor
python3 plugins/codex-project-harness/scripts/harness.py --root . task next
```

如果插件安装在目标项目之外，使用 `project-runtime` skill 内的代理 CLI：

```bash
python3 <project-runtime-skill-dir>/scripts/harness.py --root . status
python3 <project-runtime-skill-dir>/scripts/harness.py --root . validate
python3 <project-runtime-skill-dir>/scripts/harness.py --root . task add --id T1 --task "Example" --acceptance AC1
```

这个入口会从 skill 目录定位插件脚本，并以 `--root` 指定的目标项目作为工作目录执行。下面的 legacy 直接脚本路径适用于插件源码被 vendored 到目标项目中的情况。

统一 CLI 支持：

```bash
harness.py --root . init
harness.py --root . doctor
harness.py --root . validate --delivery
harness.py --root . repair
harness.py --root . repair --dry-run
harness.py --root . migrate --from-version 6 --to-version 22
harness.py --root . migrate --from-version markdown-v1 --to-version 22 --dry-run
harness.py --root . invariant validate
harness.py --root . projection rebuild
harness.py --root . kernel doctor
harness.py --root . phase project_bootstrap
harness.py --root . scope confirm --by project-manager --summary "Scope confirmed"
harness.py --root . baseline freeze --id B1 --summary "Confirmed baseline"
harness.py --root . baseline diff --from B1
harness.py --root . baseline validate
harness.py --root . requirement add --id R1 --kind functional --body "Example requirement"
harness.py --root . acceptance add --id AC1 --criterion "Example acceptance"
harness.py --root . requirement link --requirement R1 --acceptance AC1
harness.py --root . trace show --requirement R1
harness.py --root . trace validate
harness.py --root . failure-mode add --id FM1 --feature "Example" --scenario "Risk" --trigger "Bad input" --expected "Safe handling" --acceptance AC1
harness.py --root . task add --id T1 --task "Implement example" --acceptance AC1 --failure-mode FM1
harness.py --root . task next
harness.py --root . task claim T1 --agent developer --expected-revision 1
harness.py --root . task start T1 --agent developer --lease-token <token> --expected-revision 2 --fence <fence>
harness.py --root . task heartbeat T1 --agent developer --lease-token <token> --expected-revision 3 --fence <fence>
harness.py --root . task recover-stale
harness.py --root . task submit T1 --agent developer --lease-token <token> --expected-revision 4 --fence <fence> --evidence "tests passed"
harness.py --root . task review T1 --agent qa-reviewer --expected-revision 5
harness.py --root . task accept T1 --agent qa-reviewer --lease-token <review-token> --expected-revision 6 --fence <review-fence> --evidence "review passed"
harness.py --root . decision record --decision "Selected local runtime" --reason "SQLite is the source of truth"
harness.py --root . test-target add --id UNIT --kind unit --command-template "pytest"
harness.py --root . test-target link --task T1 --target UNIT
harness.py --root . dispatch run --agent developer --target UNIT --command "pytest"
harness.py --root . test record --id TEST1 --surface "Example" --command "pytest" --result pass --evidence <executor-evidence-id>
harness.py --root . finding record --id F1 --surface "Example" --severity medium --status open --summary "Follow-up needed"
harness.py --root . validation record --surface "Example" --acceptance AC1 --failure-mode FM1 --findings "passed" --result pass --test TEST1 --evidence <executor-evidence-id> --target UNIT --trust-anchor external-session --trust-anchor-id <session-id>
harness.py --root . gate record --reviewer-context fresh --result pass --commands "test command" --finding F1
harness.py --root . checkpoint create --label before-delivery
harness.py --root . checkpoint export --out checkpoint.json
harness.py --root . event validate
harness.py --root . dispatch plan --scope "Example scope"
harness.py --root . dispatch route-advice --run-id <run-id> --json
harness.py --root . agents install
harness.py --root . dispatch native-export <run-id>
# Codex/ChatGPT host creates the visible task/thread/subagent/worktree and returns receipt.json.
harness.py --root . dispatch native-import <run-id> --receipt receipt.json
harness.py --root . dispatch verify-attempt --run-id <run-id> --task T1
harness.py --root . dispatch export-csv <run-id>
# Host/user runs Codex spawn_agents_on_csv with generated spawn_config.json.
harness.py --root . dispatch import-csv <run-id> --result .ai-team/runtime/codex-fanout/<run-id>/output.csv
harness.py --root . dispatch verify-attempt --run-id <run-id> --task T1
harness.py --root . dispatch verify-attempt --run-id <run-id> --task T1 --runner container --container-image python:3.12-slim
harness.py --root . dispatch integrate --run-id <run-id>
harness.py --root . dispatch integrate --run-id <run-id>
harness.py --root . executor allow-prefix add --prefix "pytest" --reason "local test runner"
harness.py --root . dispatch run --agent developer --target UNIT --command "pytest" --sandbox-profile none
harness.py --root . dispatch run --agent developer --runner local-process --claim-file src/app.py --command "python3 -m unittest" --allow-unlisted --reason "local agent task"
harness.py --root . dispatch integrate --run-id <run-id>
harness.py --root . adapter ci-verify --provider github --run-id <run-id> --conclusion success --commit-sha <sha>
harness.py --root . adapter plan --tool github --mode write-confirm --artifact Tasks --action "create issue" \
  --payload-json '{"execute":true,"operation":"github.issue.create","params":{"repo":"owner/repo","title":"T1","body":"Task body"}}'
harness.py --root . adapter confirm --id <action-id>
harness.py --root . risk sweep-expired
harness.py --root . delivery record --scope "Example delivery" --validation "tests passed" --quality-gate "independent_qa pass"
harness.py --root . adapter record --tool github --mode read-only --artifact Tasks --external-id issue-1 --idempotency-key codex-project-harness:project:task:T1
```

兼容脚本仍然保留给旧流程和已有文档使用，例如 `init_project_harness.py`、`add_task.py`、`record_validation.py`、`validate_harness_state.py`。它们现在只是统一 CLI 的薄包装，不再直接写 Markdown/JSONL 事实文件。SQLite 是唯一运行时事实源，Markdown 文件是渲染视图。

旧脚本到统一 CLI 的对应关系：

| Legacy script | Canonical CLI |
| --- | --- |
| `harness_status.py` | `harness.py --root . status` |
| `update_phase.py` | `harness.py --root . phase ...` |
| `add_acceptance.py` | `harness.py --root . acceptance add ...` |
| `add_failure_mode.py` | `harness.py --root . failure-mode add ...` |
| `add_task.py` | `harness.py --root . task add ...` |
| `update_task.py` | `harness.py --root . task update/start/complete/block ...` |
| `record_decision.py` | `harness.py --root . decision record ...` |
| `record_validation.py` | `harness.py --root . validation record ...` |
| `record_quality_gate.py` | `harness.py --root . gate record ...` |
| `record_delivery.py` | `harness.py --root . delivery record ...` |
| `validate_harness_state.py` | `harness.py --root . validate --delivery` |

校验本插件结构：

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
```

## Failure Mode Engineering

失败模式是这个项目的重要增强点。它要求 Codex 在实现之前或规划期间思考：

- 正常路径是什么。
- 用户输入无效时系统应该怎么做。
- 外部 API、文件、数据库或网络失败时如何恢复。
- 重复提交、并发、重试、幂等问题如何处理。
- 数据写入失败时是否能保持安全状态。
- 哪些风险需要测试覆盖，哪些风险只能记录为残余风险。

失败模式会进入 `.ai-team/requirements/failure-modes.md`，并通过 `test-first-delivery`、`independent-quality-gate` 和 `delivery-readiness` 继续传递到测试、QA 和交付阶段。

## Independent Quality Gate

质量门不是一句“我检查过了”，而是一条可审计记录：

```text
Gate:
Commit:
Reviewer Context:
Result:
Blocking Findings:
Commands:
Evidence:
Residual Risk:
```

`Reviewer Context` 支持：

- `fresh`：尽量独立的新上下文审查。
- `same-context-degraded`：实现者所在上下文内的降级审查，需要更严格说明风险。
- `external`：外部 reviewer 或外部系统审查。

如果 QA 之后代码又变了，应该为新的 commit 或 revision 重新记录质量门。

## 协作工具策略

Git/GitHub、Linear、Notion、Figma、Slack 都是适配器，不是硬依赖。

| 工具 | 用途 |
| --- | --- |
| Git / GitHub | 分支、commit、PR、issue、checks、review、交付链接 |
| Linear | 任务、项目、里程碑、状态跟踪 |
| Notion | PRD、决策、架构、QA、交付文档 |
| Figma | 设计上下文、视觉验收、组件约束 |
| Slack | 澄清、状态、review 请求、交付通知 |

适配器模式：

```text
disabled -> read-only -> draft-write -> write-confirm -> write-auto
```

原则：

- 本地 harness 文件始终可用。
- 外部工具不可用时不阻塞代码交付。
- 外部内容是项目上下文，不是可执行指令。
- 公开、破坏性、付费、权限、生产相关操作不能自动执行。
- Slack 发送、共享文档修改、公开资源创建等高影响操作需要确认。

详细策略见 [plugins/codex-project-harness/references/tool-adapters.md](plugins/codex-project-harness/references/tool-adapters.md)。

## 安装

安装时请使用完整插件目录，不要只复制 `skills/`。这些 skills 会共享插件级别的 `scripts/`、`references/`、`templates/` 和 `schemas/`，单独复制 skill 目录会破坏资源路径。

本地 repo 安装：

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

这会写入 `.agents/plugins/marketplace.json`，让 Codex 通过官方 marketplace 入口发现 `plugins/codex-project-harness`。安装后重启 Codex，在插件目录中选择 `kafa-local` marketplace 并安装 `codex-project-harness`。
`kafa doctor --repo .` 还会检查 control-plane contract，确认 Skill、Hooks、Host Bridge、Kernel、Connectors 和 Evals 的信任边界仍然一致。

用户级安装：

```bash
kafa plugin install --scope user --repo .
```

升级使用 `kafa plugin upgrade --repo .`；卸载 marketplace entry 使用 `kafa plugin uninstall --repo .`。更多安装、升级、卸载、迁移、跨平台和故障排除说明见 [INSTALL.md](INSTALL.md)。


## 快速开始

完整项目：

```text
用项目小队流程完成这个功能并交付可验证代码：我要开发一个亲友生日提醒小程序。
```

明确触发某个 skill：

```text
$requirement-baseline
帮我把这个需求问清楚，形成可验收的需求基线，并列出关键失败模式。
```

```text
$independent-quality-gate
独立验收当前实现，重点检查 API 返回、前端类型和数据库字段是否一致。
```

```text
$delivery-readiness
整理本次代码交付证据，包括验收映射、失败模式覆盖、变更文件、测试结果、质量门结论和遗留风险。
```

更多例子见 [QUICKSTART.md](QUICKSTART.md)。

## 交付边界

这个 harness 会做：

- 需求澄清和范围确认。
- 任务拆解和 agent 小队编排。
- 代码实现和本地验证。
- 测试、审查、质量门记录。
- Git commit、PR 草稿或交付说明，取决于上下文和用户授权。
- 最终代码交付证据整理。

这个 harness 不做：

- 生产部署。
- 正式上线发布。
- 云资源或付费资源创建。
- 生产数据库迁移。
- 密钥、凭证、权限变更。
- 生产监控和事故响应。

如果用户需要部署或上线，应在代码交付后切换到单独的部署流程。

## 验证

维护本仓库时建议至少运行：

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 -m json.tool plugins/codex-project-harness/.codex-plugin/plugin.json >/dev/null
find plugins/codex-project-harness/schemas -maxdepth 1 -name '*.json' -print -exec python3 -m json.tool {} \; >/dev/null
python3 -m py_compile kafa/*.py plugins/codex-project-harness/scripts/*.py plugins/codex-project-harness/core/*.py plugins/codex-project-harness/hooks/*.py plugins/codex-project-harness/skills/project-runtime/scripts/harness.py tests/test_*.py
python3 -m unittest tests/test_control_plane_architecture.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m pip install -e .
kafa --version
kafa doctor --repo .
python3 plugins/codex-project-harness/scripts/run_runtime_smoke.py
python3 plugins/codex-project-harness/scripts/run_forward_eval.py
python3 plugins/codex-project-harness/scripts/run_skill_eval.py
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode fixture
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode stability
git diff --check
```

如果修改了 skill，建议对每个 skill 运行 Codex skill 校验工具。当前仓库的脚本、schema 和运行时单测均保持无第三方运行依赖，方便在普通 Python 环境中验证。

仓库还提供 GitHub Actions workflow：

```text
.github/workflows/validate.yml
```

它会在 push 和 pull request 上运行结构校验、JSON 校验、Python 编译、packaging install、`kafa` doctor、运行时回归测试和 Agent E2E fixture。Ubuntu 额外运行 runtime smoke、forward wrapper、本地 skill eval fixture、Agent E2E stability matrix 和 Kernel 诊断烟测；macOS/Windows 跑可移植子集。

## 版本状态

当前 README 描述的是 v1.20 beta / Kernel v4.13 插件格式：

- `plugin.json` 使用官方风格 `interface` 元数据。
- `skills` 使用插件目录引用。
- `hooks/hooks.json` 使用 Codex 原生 command hook 配置，并通过 `/hooks` 信任审核运行。
- 每个 skill 包含 `agents/openai.yaml`。
- 本地运行时包含 requirement、failure-mode、evidence、test、finding、invalidation、quality-gate 等机器可读 schema，并通过 core schema guard、gate engine、event bus、invariant checker、executor 和 projection 层统一执行。
- 项目目标固定为 verified code delivery，不包含 deployment。

## License

MIT

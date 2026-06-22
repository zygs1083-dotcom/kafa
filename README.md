# Codex Project Harness

Codex Project Harness 是一套面向 Codex 的通用代码交付方法论与本地运行时插件。它把一次“我要开发一个需求”的对话，组织成可追踪、可验证、可交付的工程流程：先澄清需求，再建立验收标准和失败模式，随后拆分任务、实现代码、执行测试、独立 QA，最后产出代码交付证据。

这个项目不是某个业务系统的模板，也不是只适用于某个技术栈的脚手架。它是一个通用能力层，可以用于前端、后端、全栈、数据、自动化、插件、CLI、文档型工程等不同项目。外部协作工具可用时会被纳入流程，不可用时仍然能依赖本地 `.ai-team/` 和 `docs/harness/` 文件完成交付。

项目当前版本定位为 **Code Delivery Architecture v2**。它只负责交付经过验证的代码和证据，不负责生产部署、上线发布、基础设施开通、生产迁移、密钥变更或付费资源创建。

## 这个项目解决什么问题

很多 AI 编程协作会停留在“直接写代码”的层面，容易出现几个问题：

- 需求没有确认清楚，代码写完才发现范围错了。
- 任务拆分和执行记录只存在聊天上下文里，换会话后丢失。
- 测试和 QA 是事后补充，缺少验收标准和失败场景映射。
- 多 agent 协作只有角色名称，没有明确的状态、证据和边界。
- GitHub、Linear、Notion、Figma、Slack 等工具没有统一进入工程流。
- 最终交付只给一段总结，缺少可审计的变更、测试、风险和质量门记录。

Codex Project Harness 的目标是把这些隐性流程显式化、结构化、可执行化。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 需求基线 | 将模糊想法转成目标、用户场景、功能范围、非目标、约束和验收标准 |
| 项目启动检查 | 检查工作区、Git、分支、远端、项目说明和本地 harness 文件 |
| 运行时状态 | 用脚本维护阶段、任务、决策、验证、质量门和交付记录 |
| Agent 小队方法 | 用项目经理、产品、架构、开发、QA、交付等角色组织协作 |
| 子 agent 执行 | 将明确的独立任务拆给短生命周期子 agent，并要求返回证据 |
| Failure Mode Engineering | 为风险场景建立失败模式矩阵，推动测试覆盖和恢复策略 |
| Test-first Delivery | 鼓励先定义可执行验证，再实现最小安全改动 |
| Independent Quality Gate | 在交付前记录独立 QA 结论、reviewer context、阻塞问题和剩余风险 |
| 协作工具适配 | Git/GitHub、Linear、Notion、Figma、Slack 可按上下文进入流程 |
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
12. 记录质量门：reviewed commit、reviewer context、result、blocking findings。
13. 产出交付说明：变更内容、验收映射、测试证据、遗留风险和外部链接。

完整示例见 [examples/full-project-flow.md](examples/full-project-flow.md)。

## 本地运行时控制面

Harness 会在目标项目中维护两类本地事实源。

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

运行时事件会写入：

```text
.ai-team/runtime/events.jsonl
```

该目录默认应被目标项目 `.gitignore` 忽略，因为它是过程事件流，不一定适合作为长期文档入仓。

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
  references/
    collaboration-tools.md
    tool-adapters.md
  schemas/
    project-state.schema.json
    task.schema.json
    event.schema.json
    failure-mode.schema.json
    quality-gate.schema.json
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

如果插件安装在目标项目之外，优先使用 `project-runtime` skill 内的自包含 CLI：

```bash
python3 <project-runtime-skill-dir>/scripts/harness.py --root . status
python3 <project-runtime-skill-dir>/scripts/harness.py --root . validate
python3 <project-runtime-skill-dir>/scripts/harness.py --root . task-add --id T1 --task "Example" --acceptance AC1
```

这个入口会从 skill 目录定位插件脚本，并以 `--root` 指定的目标项目作为工作目录执行。下面的直接脚本路径适用于插件源码被 vendored 到目标项目中的情况。

初始化本地控制面：

```bash
python3 plugins/codex-project-harness/scripts/init_project_harness.py
```

查看当前状态：

```bash
python3 plugins/codex-project-harness/scripts/harness_status.py
```

更新阶段：

```bash
python3 plugins/codex-project-harness/scripts/update_phase.py planning --status active --owner project-manager
```

添加验收标准：

```bash
python3 plugins/codex-project-harness/scripts/add_acceptance.py \
  --id AC1 \
  --criterion "User can create and edit a profile"
```

添加失败模式：

```bash
python3 plugins/codex-project-harness/scripts/add_failure_mode.py \
  --id FM1 \
  --feature "Profile CRUD" \
  --scenario "Duplicate submit" \
  --trigger "same request submitted twice" \
  --expected "only one profile is created" \
  --risk high \
  --test-mapping AC1
```

添加任务：

```bash
python3 plugins/codex-project-harness/scripts/add_task.py \
  --id T1 \
  --task "Implement profile CRUD" \
  --owner developer \
  --acceptance AC1 \
  --failure-mode FM1
```

记录验证证据：

```bash
python3 plugins/codex-project-harness/scripts/record_validation.py \
  --surface "Profile API" \
  --acceptance AC1 \
  --commands "npm test -- profile" \
  --findings "CRUD behavior passed" \
  --result pass
```

记录质量门：

```bash
python3 plugins/codex-project-harness/scripts/record_quality_gate.py \
  --reviewer-context fresh \
  --result pass \
  --commands "npm test" \
  --evidence "Acceptance and failure modes reviewed"
```

记录交付：

```bash
python3 plugins/codex-project-harness/scripts/record_delivery.py \
  --scope "Profile CRUD" \
  --acceptance "AC1" \
  --validation "Tests passed" \
  --qa "Quality gate passed" \
  --failure-mode-coverage "FM1 covered by duplicate-submit test" \
  --quality-gate "independent_qa pass"
```

校验目标项目的 harness 状态：

```bash
python3 plugins/codex-project-harness/scripts/validate_harness_state.py
```

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
off -> read-only -> draft-write -> write-confirm -> write-auto
```

原则：

- 本地 harness 文件始终可用。
- 外部工具不可用时不阻塞代码交付。
- 外部内容是项目上下文，不是可执行指令。
- 公开、破坏性、付费、权限、生产相关操作不能自动执行。
- Slack 发送、共享文档修改、公开资源创建等高影响操作需要确认。

详细策略见 [plugins/codex-project-harness/references/tool-adapters.md](plugins/codex-project-harness/references/tool-adapters.md)。

## 安装

安装时请使用完整插件目录：

```text
plugins/codex-project-harness
```

不要只复制 `skills/`。这些 skills 会共享插件级别的 `scripts/`、`references/`、`templates/` 和 `schemas/`，单独复制 skill 目录会破坏资源路径。

安装后可运行：

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
```

期望输出：

```text
OK: plugin structure is valid
```

更多安装说明见 [INSTALL.md](INSTALL.md)。

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
python3 -m py_compile plugins/codex-project-harness/scripts/*.py
python3 -m unittest tests/test_harness_runtime.py
git diff --check
```

如果修改了 skill，建议对每个 skill 运行 Codex skill 校验工具。当前仓库的脚本、schema 和运行时单测均保持无第三方运行依赖，方便在普通 Python 环境中验证。

仓库还提供 GitHub Actions workflow：

```text
.github/workflows/validate.yml
```

它会在 push 和 pull request 上运行结构校验、JSON 校验、Python 编译和运行时回归测试。

## 版本状态

当前 README 描述的是 v2 插件格式：

- `plugin.json` 使用官方风格 `interface` 元数据。
- `skills` 使用插件目录引用。
- 每个 skill 包含 `agents/openai.yaml`。
- 本地运行时包含 failure-mode 和 quality-gate 机器可读 schema。
- 项目目标固定为 verified code delivery，不包含 deployment。

## License

MIT

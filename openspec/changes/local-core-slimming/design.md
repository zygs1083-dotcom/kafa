## Context

### 当前基线

本设计基于仓库 `main@adba369`：

- `VERSION=1.25.0-beta.1`，当前仍为 development candidate；
- `RUNTIME_VERSION=4.18.0`；
- `KERNEL_VERSION=4.18.0`；
- `SCHEMA_VERSION=29`；
- 最新已发布 prerelease 为 `v1.21.3-beta.1`；
- Python 代码 33,521 行，其中 Plugin 18,878 行、测试 13,251 行；
- `harness_db.py` 8,933 行、306 个顶层函数；
- 54 张运行时表、67 个索引、40 个 JSON Schema、129 个 CLI parser
  节点、12 个 Skills、5 个 Hooks。

一次空项目初始化实测约 0.27 秒，源码仓库跟踪内容约 2 MB，因此这次
瘦身的首要目标不是缩短 `git clone`，而是降低运行时状态面、回归时间、
Agent 认知负担和错误信任声明。

主要已验证成本：

- Connector 专属 43 个测试、2,127 行，约 44 秒；
- Legacy Host Provider 专属 34 个测试、1,443 行，约 47 秒；
- 每次写事务对几乎全部表生成 mutation snapshot；5,000 条事实时，单次
  写入约 0.238 秒，禁用该 journal 后约 0.018 秒；
- `PreToolUse + PostToolUse` 每个工具往返约增加 0.11 秒；
- 空数据库约 540 KiB，并生成外部工具专属 projection。

### 根本矛盾

产品现在只需要本地代码交付，但实现仍同时承担：

1. 五套外部系统直连协议及其授权之外的治理状态；
2. 一套与 Native Codex 重叠的 Host SDK agent lifecycle；
3. 面向多写者的 lease/fence/heartbeat 协调；
4. 面向事件重放的全库 mutation journal；
5. Evidence、Test、Validation 三份重复命令事实；
6. 以 Connector HMAC 冒充独立高信任来源的本地 gate。

Native Codex/ChatGPT 已经拥有 task、thread、subagent、worktree、approval、
model、cancel、steer 和 handoff。Kafa 不应继续维护第二套 agent 平台。

### 目标架构

```text
Natural-language Skills + Project AGENTS
                  |
                  v
        Native Codex / ChatGPT Host
        - task/thread/subagent/worktree
        - approval/model/cancel/handoff
                  |
                  | code candidate + self-reported context ids
                  v
          Kafa Local Delivery Kernel
        - cycle/requirement/acceptance/risk
        - task intent and current candidate
        - controller execution facts
        - findings/review/delivery decision
                  |
                  v
      Local Git or content identity + SQLite
        + optional no-network container
```

外部 Apps、MCP 或网站仍可由用户直接使用，但不再进入 Kafa runtime、
schema、delivery trust 或发布声明。

## Goals / Non-Goals

**Goals:**

- 发布一个明确 breaking 的 `v2.0.0-beta.1` 本地内核代际；
- 设置 `RUNTIME_VERSION=5.0.0`、`KERNEL_VERSION=5.0.0`、
  `SCHEMA_VERSION=30`；
- 彻底删除业务项目运行时对 GitHub、Linear、Notion、Figma、Slack、
  `gh`、外部 bearer token 和 provider HTTP API 的调用；
- 删除 Legacy Host Codex SDK、watchdog、Spark 环境路由和 CSV/provider
  compatibility lifecycle；
- 让 Native Codex 成为唯一 agent lifecycle owner；
- 把 active schema 收缩到 27 张核心表；
- 把 CLI 收缩到不超过 60 个 parser 节点；
- 把 Skills 收缩到不超过 8 个、Hooks 收缩到 3 个；
- 把测试执行事实存储一次，Validation 通过外键引用；
- 删除每次事务的全库扫描，保持写成本与本次变更行数相关；
- 保留 schema 27/28/29 到 schema 30 的可恢复迁移路径；
- 保留当前 cycle/current candidate、结构化测试、finding、QA 和 delivery
  的 fail-closed 语义。

**Non-Goals:**

- 不删除本地 Git；
- 不删除维护 Kafa 自身使用的 GitHub Actions、tag 或 release workflow；
- 不实现新的远程 Connector、Apps/MCP receipt broker 或云端状态传输；
- 不实现 Kafa 自己的 subagent、worktree、approval 或 model selector；
- 不声称本地模型进程生成的 token、session id 或文本是密码学证明；
- 不在同一波次重新设计 OpenSpec；OpenSpec 继续是 proposal/design/tasks
  的 spec 层，Kafa 只保留交付所需的最小 acceptance facts；
- 不以保持 v1 CLI 完全兼容为目标；仅保证数据可备份、可迁移、可回滚。

## Decisions

### 1. 使用 major beta 表达收缩，而不是继续堆叠 v1 compatibility

目标版本固定为：

```text
VERSION=2.0.0-beta.1
RUNTIME_VERSION=5.0.0
KERNEL_VERSION=5.0.0
SCHEMA_VERSION=30
branch=v2-local-core-slimming
```

原因：本次会删除公开命令、表、schema、Skills、Hooks 和 optional
dependency。继续沿用 v1 minor 会掩盖真实 breaking boundary。

替代方案是保留所有旧命令作为 deprecated stub。该方案会继续扩大 parser、
文档和测试面，因此只允许在 `migrate` 输出中给出替代指引，不在主 CLI
长期保留空壳命令。

### 2. 运行时严格 local-only，维护基础设施不受影响

业务项目的 Plugin runtime 不得导入或调用 provider-specific Connector
client。`urllib` 仅允许用于与外部工具无关的测试或安装用途；Plugin Kernel
不得执行 `gh api` 或访问 Linear、Notion、Figma、Slack endpoint。

Kafa 自身仍可通过 GitHub Actions 做三平台测试和 prerelease 发布。这属于
仓库维护，不是业务项目 runtime integration。

### 3. 删除 Connector 子系统而不是改成 feature flag

删除以下 active runtime facts：

- `adapters`；
- `adapter_actions`；
- `connector_budgets`；
- `connector_profiles`；
- `advisory_fallbacks`；
- `ci_verifications`；
- `external_session_verifications`；
- `project.connector_project_key`；
- requirement/acceptance/task 的 `tool_link`；
- delivery 的 `collaboration_links`；
- Connector HMAC key、origin 和 trust branches。

同时删除 `connector`、`adapter` CLI、外部 projection、Connector schemas、
fake servers、专属 tests、stability scenarios、Apps/MCP Receipt ADR 和
collaboration references。

选择删除而不是默认关闭，是因为 feature flag 仍会保留 schema、代码、测试、
文档和信任耦合，无法达到瘦身目标。

### 4. Native Codex 是唯一 agent lifecycle owner

删除：

- `HostCodexProvider` 和 `openai-codex` optional dependency；
- background worker、watchdog、atomic report、process tree cancellation；
- `HARNESS_CODEX_*` model/Spark/legacy host policy；
- fixture provider lifecycle；
- `dispatch provider start/status/collect/cancel/reconcile`；
- `dispatch export-csv/import-csv`；
- `dispatch native-export/native-import` 手工 receipt exchange；
- Kafa-owned worktree create/merge/integrate/file-claim lifecycle。

Native Codex 在自己的可见 task/worktree 中完成代码变更。根控制器回到目标
workspace 后，仅对当前 candidate 运行 Kafa controller verification。

Kafa 可在 task 或 quality gate 中保存自报的 producer/reviewer host context
id，但必须标记为 audit metadata，不能升级为密码学 trust anchor。

### 5. 根控制器单写，Task 状态机取消伪分布式协调

目标 Task 状态：

```text
planned -> active -> submitted -> accepted
                    |            -> blocked
                    -> blocked
planned/active/submitted -> cancelled
```

保留 `revision` 作为可读审计序号，但删除 lease token、heartbeat、expiry、
retry budget、fence、claim/release/recover-stale 和 reviewer lease。

目标 Task CLI：

```text
task add
task list
task start
task submit
task accept
task block
task cancel
```

Producer/reviewer 分离由 `submitted_context_id` 与 quality gate 的
`reviewer_context_id` 比较。该比较证明流程分离声明，不证明宿主身份不可伪造。

### 6. 统一 Execution、Validation 和 Delivery facts

新增不可变 `executions` 表，只有 controller executor 可以写入：

```text
id, cycle_id, candidate_sha, target_id, command,
exit_code, stdout_sha256, artifact_path,
executed_count, result_format, semantic_status,
runner, sandbox_status, no_network, policy_status,
created_at
```

`executions` insert-only；相同 execution id 不允许 update。删除通用
`evidence record` 和 `test record` 写入口，避免手填 command facts。

`validations` 只保存 judgment：

```text
id, cycle_id, candidate_sha, acceptance_id, surface,
result, validation_status, superseded_by,
findings, residual_risk, created_at
```

通过 `validation_executions` 关联一个或多个 execution；通过
`validation_failure_modes` 关联风险。Delivery gate 必须读取 execution 原始
事实，不能从 Validation 复制字段。

目标验证命令：

```text
verify run --target TARGET [--acceptance AC] [--failure-mode FM]
           [--runner local|container] [--container-image IMAGE]
```

成功时在同一事务写 execution、validation links 和 audit event。运行命令本身
在事务外执行。

### 7. schema 30 只包含 27 张 active tables

目标表集合：

```text
project
delivery_cycles
requirements
acceptance
requirement_acceptance
failure_modes
failure_mode_acceptance
baselines
tasks
task_acceptance
task_failure_modes
task_dependencies
test_targets
task_test_targets
executions
validations
validation_executions
validation_failure_modes
findings
quality_gates
quality_gate_findings
deliveries
delivery_acceptance
decisions
invalidations
migrations
events
```

不为了兼容在 greenfield schema 30 中创建 retired tables。旧数据只存在于
pre-migration backup。

### 8. 高风险本地模式必须诚实，不再使用同进程 HMAC

本地信任等级简化为：

- `controller-verified`：controller 对当前 candidate 执行的 test target；
- `reviewed-local`：不同自报 context id 的 reviewer 给出的本地 review；
- `same-context-degraded`：同一 context 的降级审查，仅允许 low/medium；
- `human-review-required`：high/critical 默认结果，不是通过状态。

High/critical active failure mode 必须满足：

1. 关联当前 candidate 的 structured execution；
2. target 要求 sandbox/no-network 时实际 metadata 必须满足；
3. reviewer context 与 producer context 不同；
4. 没有 host-verifiable receipt 时，Kernel 返回 `human-review-required`，
   不自动记录 delivered。

用户可显式接受/豁免风险，但必须记录 actor、reason、scope、revision 和 expiry。
该事实是 procedural audit，不描述为密码学证明。所有 high/critical 都被显式
接受或豁免后，delivery 才可按 accepted-risk 路径继续。

### 9. 保留 Events，删除全库 event sourcing

`events` 继续作为 append-only audit log，但删除：

- 事务前后的 `replay_mutation_snapshot()`；
- `canonical_mutations` 全库 diff；
- `runtime_snapshots` 表；
- event replay/rebuild；
- JSON checkpoint import/export；
- public `event validate/export` 和 `checkpoint` CLI。

恢复改用 SQLite backup API：migration 前自动备份，管理员可通过内部 helper
创建一致性 backup。Audit event 只记录当前命令的 entity id、before/after 摘要
和 actor，不承担完整状态重建承诺。

### 10. Projection 按受影响视图更新

事务提交后不再无条件 `render_all()`。每个 mutation 返回受影响 projection
集合，例如 requirement 只重建 requirements/traceability/project-state。

`projection rebuild` 可保留为 admin recovery command，但普通写操作不得打开
十余次连接并重写全部 Markdown。

删除默认生成：

- `.ai-team/control/tooling-map.md`；
- `.ai-team/control/advisory-fallbacks.md`；
- 外部链接列和 Connector project key。

### 11. CLI、Skills、Agents 和 Hooks 收缩

目标顶层 CLI 领域：

```text
init, status, doctor, quickstart
cycle, requirement, acceptance, failure-mode, baseline, trace
task, test-target, verify, validation
finding, gate, delivery, decision
validate, repair, migrate, projection
```

目标 parser 节点不超过 60。删除 request-id global command log；本地 mutation
依靠 SQLite transaction、natural key upsert 和 explicit state precondition 保持
幂等。

目标 Skills 最多 7 个：

1. `project-harness`；
2. `minimal-safe-change`；
3. `bug-fix-loop`；
4. `test-first-delivery`；
5. `independent-quality-gate`；
6. `harness-audit`；
7. `project-retrospective`。

合并或删除 `project-bootstrap`、`project-runtime`、`requirement-baseline`、
`team-architecture`、`delivery-readiness` 独立 Skill；其必要内容进入主 Skill、
OpenSpec 路由或内部 reference。

默认 Agent templates 最多 3 个：developer、architect、qa-reviewer。不要在每个
项目初始化时安装 bootstrap/product/delivery coordinator。

目标 Hooks：

- `SessionStart`：一次性读取本地 status；
- `SubagentStart`：注入任务/证据边界；
- `Stop`：warn-only readiness。

删除默认 `PreToolUse` 和 `PostToolUse`。写入约束由 AGENTS、宿主 approval 和
Kernel gate 共同承担。

### 12. 模块边界以职责而不是行数阈值冻结

目标内部模块：

```text
core/schema_lifecycle.py   schema 30 create/migrate/backup
core/store.py              concrete SQLite connection/transaction
core/ledger.py             cycle/requirement/task/finding facts
core/execution.py          local/container execution and parsing
core/delivery.py           candidate-scoped readiness decision
core/projections.py        targeted generated views
core/api.py                explicit public Python API
scripts/harness.py         argparse and output only
```

`harness_db.py` 可以在迁移期作为薄 compatibility facade，最终不得继续承载
Connector、provider、executor、migration、projection 和 gate 的全部实现。

Freeze tests 应冻结：

- 目标 public CLI；
- schema 30 table set；
- migration compatibility；
- delivery negative invariants；
- plugin manifest、安装与 release contract。

不再用 `harness_db.py < 9000` 这种阈值证明模块化。

### 13. Evals 只证明本地产品声明

删除 Connector mock、Connector exactly-once、fake Host SDK、Spark policy、
provider crash recovery 等 scenario。

新的 stability matrix 至少覆盖：

1. fresh local install and init；
2. quickstart minimal stops before independent review；
3. current candidate validation supersedes stale validation；
4. forged/manual command evidence cannot satisfy delivery；
5. open high/critical finding blocks delivery；
6. high-risk local flow returns `human-review-required`；
7. structured result and no-network policy fail closed；
8. cycle isolation；
9. SQLite single-writer contention has no lock leakage；
10. schema 27/29 -> 30 migration and rollback；
11. installed Plugin/Skill/Hook discovery；
12. opt-in real Codex host can edit code and controller verifies the resulting
    current candidate without Kafa-owned provider lifecycle。

### 14. Spark 子 agent 仅承担确定性小任务

主模型必须负责：schema migration、trust policy、delivery gate、execution
normalization、public API removal 和最终整合。

只有满足以下全部条件的任务才可交给 Spark 子 agent：

- 任务边界已由主模型锁定；
- 最多约 1-3 个文件；
- 不涉及 schema、trust、migration 或跨模块决策；
- 有确定的测试或 `rg` inventory 作为验收；
- 失败不会破坏 active migration 或用户数据。

合适示例：删除已经断开的专属测试文件、更新固定 inventory、清理文档中的
旧命令、调整单个 JSON schema。主模型必须复验其 diff 和测试结果。

## Performance Budgets

以下为验收预算，不要求在波动较大的公共 CI 上做严格 wall-clock assertion，
但必须生成可比较 benchmark report：

| 指标 | 当前 | 目标 |
| --- | ---: | ---: |
| Active tables | 54 | 27 |
| CLI parser nodes | 129 | <= 60 |
| Skills | 12 | <= 7 |
| Hooks | 5 | 3 |
| Empty DB | ~540 KiB | <= 320 KiB |
| Plugin directory | ~1.4 MiB | <= 1.0 MiB |
| 5k-fact single mutation | ~0.238 s | <= 0.050 s reference machine |
| Full unittest | ~592 s prior verified run | <= 300 s reference Linux job |
| Direct external runtime calls | 5 providers | 0 |

代码目标：总体 Python 与 tests 减少 35%-45%，不得通过删除 delivery negative
coverage 来达成。

## Migration Plan

### Supported source states

- Greenfield：直接创建 schema 30；
- Schema 29：直接执行 local-core migration；
- Schema 27/28：先调用只读隔离的 legacy migration code 转换到 schema 29
  staging DB，再转换到 schema 30；
- Schema 6-26：仅在已有迁移 fixture 证明可到 schema 29 时支持，否则明确
  fail closed 并要求先安装最后一个 v1 release；
- Unknown/newer schema：拒绝。

### Migration algorithm

1. 获取 project-level migration lock；
2. 运行 source DB integrity check、foreign key check 和 schema version CAS；
3. 使用 SQLite backup API 写入
   `.ai-team/backups/schema-<source>-before-local-core-<timestamp>/harness.db`；
4. 在同目录创建 `harness.schema30.new.db`；
5. 创建纯 schema 30；
6. 按依赖顺序复制 local facts，并显式转换 execution/validation/task 状态；
7. 不复制 Connector、adapter、external trust、provider、dispatch、worktree、
   report、fanout、snapshot 或 command-log rows；
8. 写 migration manifest，包含 source/target version、row counts、dropped table
   counts、backup path 和 digest，不包含 token 或原始外部 payload；
9. 对新 DB 运行 schema、foreign key、invariant 和 projection dry-run；
10. fsync 后原子替换 active DB；
11. 重建本地 projections；
12. 最终 doctor/quickstart status 通过后才记录 migration success。

### Fact conversion

- `evidence` 中由 controller 生成且 artifact/hash/current-candidate 校验通过的
  command row 转为 `executions`；
- manual/policy/empty evidence 不转成 gate-eligible execution，只在 migration
  manifest 计数；
- `validations` 保留 judgment 和 supersession，并通过旧 validation_evidence
  建立 `validation_executions`；
- 无法绑定 execution 的旧 validation 保留为 `invalidated` audit row，不能
  满足 delivery；
- current cycle、requirements、acceptance、failure modes、tasks、findings、
  gates、deliveries、decisions 和 invalidations 按 cycle/candidate 保留；
- provider/dispatch/external records 仅保留于完整 backup。

### Rollback

- 任何替换前失败：删除 `.new.db`，active schema 保持不变；
- 替换后 doctor 失败：自动把 schema 30 DB 移到 failed-migration artifact，
  从 verified backup 恢复 active DB；
- 已在 schema 30 写入新事实后不支持自动 downgrade；用户可显式恢复 backup，
  但会丢失升级后的新事实，CLI 必须警告并要求确认 backup digest；
- 不允许在 rollback 时把 removed external/provider rows导入 schema 30。

## Implementation Waves

### Wave 0: Contract and red tests

- 固定 v2 public surface、27-table schema、migration fixtures 和 negative gates；
- 先让 removed external/legacy commands、same-process HMAC、高风险 auto-delivery、
  全库 snapshot 等测试按新契约失败；
- 不删除 production code。

Checkpoint：红测失败原因与本设计逐项对应，现有 v1 baseline 保持可运行。

### Wave 1: Schema 30 and trust foundation

- 实现 side-by-side migration、backup、rollback；
- 创建 schema 30 core tables；
- 实现本地 trust states 和 high-risk `human-review-required`；
- 完成 schema 27/29 fixtures。

Checkpoint：迁移失败不会改变 source DB；delivery negative tests 通过。

### Wave 2: Remove external and legacy host surfaces

- 删除 Connector、adapter、HMAC、external verification；
- 删除 Host SDK/provider/watchdog/Spark/CSV/native receipt exchange；
- 更新 API、CLI、schemas、projections 和 inventories；
- 保持 local verification path 可运行。

Checkpoint：Plugin runtime 搜索不到 provider endpoint、token env、`gh api`、
`openai_codex` 或 Host worker entrypoint。

### Wave 3: Single-writer task and execution normalization

- 简化 Task lifecycle；
- 引入 immutable executions；
- Validation 改为引用 execution；
- `verify run` 替代 dispatch/evidence/test manual paths；
- gate 只读取 normalized facts。

Checkpoint：最小本地项目可完成 requirement -> task -> verify -> review ->
delivery；manual artifact 无法伪造 execution。

### Wave 4: Remove event sourcing and target projections

- 删除 full DB mutation snapshots、runtime snapshots 和 replay；
- 保留 compact audit events 和 SQLite backup；
- 按受影响视图重建 projection；
- 完成 performance benchmark。

Checkpoint：5k-fact mutation 达到预算，doctor/invariant/projection 全通过。

### Wave 5: Product surface and documentation

- 收缩 Skills、Agent templates、Hooks；
- 更新 installer inventory、control-plane contract、README、INSTALL、
  QUICKSTART、Runtime docs 和 CHANGELOG；
- 删除过期 ADR 或标记 superseded；
- 修复所有版本/迁移示例的单一来源。

Checkpoint：isolated user install 发现精简后的 Plugin、Skills 和 Hooks；普通
项目没有外部 projection 或 Connector guidance。

### Wave 6: Evals, regression, adversarial review, release readiness

- 重建 local-only fixture/stability/live profiles；
- 三平台 full regression；
- 运行 schema rollback、forged evidence、high-risk、dirty tree、stale candidate、
  malformed structured result 和 container-unavailable 对抗测试；
- 生成 before/after metrics；
- 保持 release state 为 development，直到用户批准 tag。

Checkpoint：Definition of Done 全部满足后才允许提交 merge proposal。

## Risks / Trade-offs

- **旧自动化调用 removed CLI** -> major beta、migration manifest 和明确替代命令，
  不保留长期 stub。
- **外部审计历史不在 active DB** -> 完整 pre-migration backup 与 digest；active
  Kernel 不再承担无用查询和 schema。
- **删除 Host Provider 后失去 hidden automation** -> Native Codex 是权威；
  real-host eval 验证“宿主改代码，controller 验 candidate”的真实路径。
- **本地 reviewer id 可被文本伪造** -> 明确降级为 procedural audit；high-risk
  不自动声称 trusted/delivered。
- **一次性 schema 重构范围大** -> side-by-side DB、分波次 feature branch、每波
  checkpoint 和 rollback fixture。
- **删减测试可能掩盖回归** -> 先建立目标契约测试，再删除只覆盖 retired
  behavior 的测试；delivery negative coverage 不减。
- **Skills 过度合并降低触发精度** -> 保留 7 个用户意图明确的入口，把 runtime
  命令细节移到 main Skill reference，不放回系统 prompt。
- **GitHub Actions 仍是外部服务** -> 明确它仅维护 Kafa 自身，不进入业务项目
  runtime；若未来要求完全离线发行，另立 proposal。

## Definition of Done

- 所有 spec scenarios 有自动测试或明确 manual/live verification；
- active schema 正好 27 张表，无 retired connector/provider tables；
- CLI parser nodes <= 60，Skills <= 7，Hooks = 3；
- Plugin Kernel 不执行 GitHub/Linear/Notion/Figma/Slack 请求；
- base `pyproject.toml` 不包含 `openai-codex` extra；
- Native Codex ownership 文档与实现一致；
- schema 27、29 -> 30 成功和 rollback fixture 通过；
- high/critical 无外部/host proof 时不会自动 delivery pass；
- execution facts insert-only，manual validation 不能生成 gate-eligible command；
- transaction 不扫描全部 runtime tables；
- current candidate、structured result、sandbox/no-network、finding、reviewer
  separation 和 risk acceptance gates 不放宽；
- `py_compile`、structure validation、full unittest、runtime smoke、skill eval、
  local stability、isolated install、opt-in live Codex、`git diff --check` 全通过；
- before/after report 达到性能与规模预算，或记录经用户批准的具体偏差；
- 工作区只包含计划内改动，不覆盖用户未提交内容；
- 未经用户明确要求不 tag、不发布、不部署、不自动合并 main。

## Open Questions

没有阻塞实施的问题。以下决策已由本计划锁定：业务 runtime local-only；保留
本地 Git 与 Kafa 自身 GitHub CI；高风险默认不自动交付；外部/provider 历史只
保留于 migration backup；本次使用 major beta 和 schema 30。

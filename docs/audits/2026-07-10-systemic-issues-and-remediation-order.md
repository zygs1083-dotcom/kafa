# Kafa 系统性问题基线与修复顺序

## 文档状态

- 状态：`Ready for remediation planning`
- 审计日期：2026-07-10
- 审计基线：`main@c803983`
- 源码声明版本：`1.25.0-beta.1 / runtime 4.18.0 / schema 28`
- GitHub 最新正式 prerelease：`v1.21.3-beta.1`
- 适用范围：Kafa 根级安装器、Codex Plugin、Hooks、Host Provider、Kernel、SQLite runtime、Connectors、Evals、发行流程
- 本文用途：作为后续目标模式的缺陷修复输入，不是已批准的架构 ADR

## 结论

Kafa 的核心方向仍然成立：Skill 负责自然语言入口，Kernel 负责交付事实和门禁，外部执行结果不能直接成为 delivery evidence。

但当前实现存在多项可复现的 false-pass、审计历史损坏、外部重复写和全新安装失败问题。当前版本不应继续宣称“高可信交付内核”或扩大自动交付能力。在 P0 问题关闭前，应暂停新增 schema、Connector 能力、Provider 能力和正式版本发布。

修复工作的根本目标不是继续增加控制面功能，而是恢复以下四个承诺：

1. delivery gate 不会忽略失败事实或选错最新事实；
2. 高信任证据不能由被验证的同一进程自行签发；
3. 历史 cycle 和外部写入结果不会被静默改写或重复执行；
4. 用户按照官方安装文档可以在空白项目中完成真实闭环。

## 严重级别

| 级别 | 定义 |
| --- | --- |
| P0 | 可导致错误交付、伪造可信证据、审计历史损坏，或让官方主安装路径完全不可用；必须 stop-ship |
| P1 | 可导致外部重复写、越权写入、Provider 生命周期失控、迁移损坏或发布结果误导 |
| P2 | 当前不一定直接造成数据错误，但会持续制造维护成本、产品割裂和未来兼容风险 |

## 问题总表

| ID | 级别 | 问题 | 已复现 | 建议修复波次 |
| --- | --- | --- | --- | --- |
| DT-001 | P0 | open/critical finding 不阻断 delivery | 是 | Wave 2 |
| DT-002 | P0 | 同秒写入的最新 fail gate 可被旧 pass 隐藏 | 是 | Wave 2 |
| CY-001 | P0 | Delivery Cycle 的全局 ID 会搬迁旧历史 | 是 | Wave 1-2 |
| TR-001 | P0 | Connector HMAC 可由普通 CLI 自签 | 是 | Wave 2 |
| QS-001 | P0 | Quickstart 在同一进程伪造 fresh QA 并 delivered | 是 | Wave 2 |
| IN-001 | P0 | user-scope marketplace source path 无效 | 是 | Wave 3 |
| DB-001 | P1 | `executescript()` 破坏事务回滚承诺 | 是 | Wave 1 |
| DB-002 | P1 | migration 版本由调用者自报，可写入任意版本 | 是 | Wave 1 |
| CN-001 | P1 | Notion ambiguous success 可重复创建 page | 是 | Wave 4 |
| CN-002 | P1 | Linear comment/update 可越过 profile scope | 是 | Wave 4 |
| CN-003 | P1 | idempotency key 不绑定 payload | 是 | Wave 4 |
| IN-002 | P1 | project doctor 输出无法运行的业务项目命令 | 是 | Wave 3 |
| IN-003 | P1 | hooks 的标准安装 fallback 路径和版本读取错误 | 是 | Wave 3 |
| IN-004 | P1 | doctor/CI 无法发现真实插件安装失败 | 是 | Wave 3/6 |
| RL-001 | P1 | 源码版本、tag、release 和可安装版本分叉 | 是 | Wave 3/6 |
| EV-001 | P1 | `live-codex` 启用后仍永久 skipped | 是 | Wave 6 |
| EV-002 | P1 | fixture success 场景绕过真实 delivery validation | 是 | Wave 6 |
| HP-001 | P1 | Host Provider timeout/heartbeat/cancel 生命周期不可靠 | 是 | Wave 5 |
| HP-002 | P1 | Host Provider 不继承父任务权限模型 | 是 | Wave 5 |
| HP-003 | P1 | Host Provider 不是原生 subagent/thread 集成 | 是 | Wave 5 |
| ST-001 | P1 | ignored SQLite 主事实源无法自然进入 managed worktree/cloud | 设计事实 | Wave 5 |
| PK-001 | P1 | 所有安装强制下载 84-95 MB Host SDK CLI | 是 | Wave 3/5 |
| HP-004 | P2 | `manual-csv` provider 与真实 import lifecycle 脱节 | 是 | Wave 5 |
| AR-001 | P2 | `harness_db.py` 和 `core.api` 形成名义模块化 | 是 | Wave 7 |
| AR-002 | P2 | feature freeze 冻结文件和表，而不是稳定契约 | 是 | Wave 7 |
| AP-001 | P2 | Plugin 未使用 Apps/MCP，直接维护五套外部协议 | 是 | Wave 4-5 |
| MR-001 | P2 | 模型路由硬编码 Spark preview，未适配宿主原生选择 | 是 | Wave 5 |
| DC-001 | P2 | README、安装文档和版本说明存在内部漂移 | 是 | Wave 3/7 |

## P0：必须先关闭的问题

### DT-001：结构化 open/critical finding 不阻断 delivery

**现象**

Quality gate 可以通过 `quality_gate_findings` 关联结构化 finding，但 delivery readiness 只检查 `quality_gates.blocking_findings` 自由文本。关联的 open critical finding 不会自动成为 blocker。

**代码证据**

- `plugins/codex-project-harness/scripts/harness_db.py:3802`：只建立 gate 与 finding 的关联。
- `plugins/codex-project-harness/core/gate_engine.py:346`：只读取自由文本 `blocking_findings`。

**影响**

- 已知 critical finding 可以与 passing quality gate 同时存在；
- delivery gate 返回成功，形成直接 false-pass；
- finding 表失去作为 Kernel fact 的意义。

**必须达到的结果**

- delivery decision 从结构化 finding 派生 blocker；
- 当前 cycle/current candidate 关联的 open critical/high finding 必须阻断；
- waived/accepted finding 必须记录 actor、reason、scope、candidate/revision 和 expiry；
- 自由文本只能补充说明，不能成为唯一门禁来源。

**必需测试**

- passing gate + open critical finding 必须失败；
- resolved finding 不阻断；
- expired waiver 恢复阻断；
- 旧 cycle finding 不阻断新 cycle。

### DT-002：最新 fail gate 可被旧 pass 隐藏

**现象**

`created_at` 只有秒级精度，quality gate ID 是随机 UUID；latest gate 使用 `order by created_at desc, id desc`。同一秒写入 pass 后再写 fail 时，随机 UUID 可能让旧 pass 排在前面。

**代码证据**

- `plugins/codex-project-harness/scripts/harness_lib.py:29`：时间戳精度不足。
- `plugins/codex-project-harness/scripts/harness_db.py:3769`：随机 UUID gate ID。
- `plugins/codex-project-harness/core/gate_engine.py:333`：用 timestamp + UUID 判断 latest。

**影响**

- 后写的失败结论可能不生效；
- delivery gate 结果不具备确定性；
- 不同 SQLite/平台运行可能得到不同结果。

**必须达到的结果**

- 对 candidate-scoped gate 使用数据库分配的严格递增 sequence/revision；
- latest 的定义不得依赖随机 ID；
- 同一 candidate 的新 gate 必须显式 supersede 旧 gate；
- pass 后写 fail 时必须稳定阻断。

**必需测试**

- 同一时间戳 pass -> fail 只能选择 fail；
- fail -> pass 只能选择后写 pass；
- 并发写入有确定的 winner 或明确冲突。

### CY-001：Delivery Cycle 不保留 cycle-scoped identity

**现象**

Requirement、acceptance、failure mode 和 task 使用全局主键。Requirement/acceptance upsert 会把旧 row 的 `cycle_id` 改为当前 cycle；task 则直接拒绝重用相同 ID，行为不一致。

**代码证据**

- `plugins/codex-project-harness/scripts/harness_db.py:459`：事实使用全局 ID。
- `plugins/codex-project-harness/scripts/harness_db.py:2427`：requirement upsert 更新 cycle。
- `plugins/codex-project-harness/scripts/harness_db.py:2458`：acceptance upsert 更新 cycle。
- `plugins/codex-project-harness/scripts/harness_db.py:2797`：task 全局重复拒绝。
- `plugins/codex-project-harness/scripts/harness_db.py:2345`：部分 QA 查询仍扫描跨 cycle active task。

**影响**

- 新 cycle 重用 `R1/AC1` 会篡改旧 cycle 审计历史；
- checkpoint、traceability 和 retrospective 无法可靠还原历史；
- 同类事实的 ID 语义不一致。

**必须达到的结果**

- cycle-owned fact 使用 `(cycle_id, local_id)` 或不可变内部 ID；
- 旧 cycle row 不允许被新 cycle upsert 修改；
- 所有 link 和 gate query 强制 same-cycle；
- schema 28 升级必须无损迁移历史。

**必需测试**

- 两个 cycle 都能拥有本地 ID `R1/AC1/T1`；
- 新 cycle 写入不改变旧 cycle dump；
- cross-cycle link fail-closed；
- migration 前后历史 row 数量和关系一致。

### TR-001：Connector HMAC 可以由同一 CLI 自行签发

**现象**

当调用者声明 `origin=connector` 且未提供 token 时，`prepare_connector_record()` 会读取 key、计算 HMAC，并把记录保存为 `hmac-valid`。签发者和验证者是同一普通 CLI 进程。

**代码证据**

- `plugins/codex-project-harness/core/connector_trust.py:76-96`。
- CLI 暴露 `--origin connector` 和可空 `--verification-token`：`plugins/codex-project-harness/scripts/harness.py:528-596`。

**复现结果**

在临时项目中，不提供外部 token 也可以创建：

- `trust_level=connector` 的 reviewer attestation；
- `token_status=hmac-valid` 的 CI verification。

**影响**

- HMAC 只证明本地记录自洽，不能证明外部独立来源；
- high/critical failure-mode trust anchor 可以被当前工作进程伪造；
- “模型拿不到 key”的安全假设没有由进程、权限或接口强制执行。

**必须达到的结果**

- Kernel CLI 永远不能生成 connector token；
- `origin=connector` 必须携带外部签发的不可空 receipt/token；
- 签发者必须位于独立进程、CI identity、OS keychain-backed broker 或远程 verifier；
- verifier 只验证，不签发；
- 旧自签记录迁移为 `local-only` 或 `legacy-untrusted`。

**必需测试**

- 空 token + connector origin 必须失败；
- agent 自建 key 文件不能获得 connector trust；
- replay 到不同 payload/candidate/session 必须失败；
- external issuer 的有效 receipt 才能通过。

### QS-001：Quickstart 自动制造 fresh QA 和 delivered

**现象**

`quickstart minimal --execute` 在同一进程里执行测试、接受 task、以字符串 `qa-reviewer` 完成 review、写 `reviewer_context=fresh` 的 pass gate，并直接记录 delivery。

**代码证据**

- `plugins/codex-project-harness/scripts/harness_db.py:8222-8249`。

**影响**

- onboarding convenience 绕过 producer/reviewer separation；
- 用户看到 delivered，会误以为独立 QA 已完成；
- 文档宣称的 trust model 与实际行为冲突。

**必须达到的结果**

- quickstart 只能生成 setup、test evidence 和待办清单；
- 不得自行声明 `fresh`、independent QA 或 delivered；
- 可提供明确标记的 `demo/audit-only` 模式，但不能满足真实 delivery gate；
- 用户必须通过独立 session/native subagent 或显式人工确认完成最终 gate。

**必需测试**

- quickstart 执行后 cycle 不能自动 delivered；
- 不存在 reviewer session 时不能记录 fresh independent gate；
- UI/CLI 明确输出下一步 reviewer 命令。

### IN-001：user-scope 安装生成无效 marketplace source

**现象**

安装器把插件复制到 `~/.agents/plugins/codex-project-harness`，但 marketplace entry 写入 `./codex-project-harness`。Codex 按 marketplace 根解析后找不到插件。

**代码证据**

- `kafa/cli.py:183-196`。
- `tests/test_install_release.py:90-108` 把错误路径固化为期望值。

**影响**

- 全新 user-scope 官方安装路径失败；
- 现有机器只有经过手工修正才能工作；
- doctor 和 CI 仍可能报告成功。

**必须达到的结果**

- 定义并测试 Codex 实际的 path resolution 规则；
- user marketplace 使用可被 `codex plugin add` 解析的路径；
- 安装器输出真实 plugin identity/version；
- upgrade/uninstall 使用同一 managed source metadata。

**必需测试**

- 隔离 HOME 中执行 install -> marketplace add -> plugin add -> plugin list；
- installed plugin 的 version 与 tag 一致；
- upgrade 和 uninstall 在隔离 HOME 中端到端通过。

## P1：高风险可靠性问题

### DB-001：Schema 创建破坏事务回滚

`SqliteStore.transaction()` 承诺异常时 rollback，但 `create_schema()` 使用 `executescript()`；SQLite 会隐式提交已有事务。审计复现中，随后强制异常仍保留了先前 insert。

- 证据：`core/store.py:87`、`scripts/harness_db.py:430`。
- 修复结果：schema lifecycle 必须拥有完整事务语义；不能在普通业务 transaction 内调用隐式 commit 操作。
- 测试：每个 migration step 注入异常后，schema version、表和业务 row 全部回到迁移前状态。

### DB-002：Migration 接受调用者伪造版本

当前 migrate 不核对数据库实际 `from_version`，也不限制 `to_version`。schema 28 数据库可接受 `6 -> 999`，保留旧表却把版本标成 999。

- 证据：`scripts/harness_db.py:7666-7687`。
- 修复结果：读取实际版本；只允许已注册、相邻或显式支持的迁移图；未知 target fail-closed。
- 测试：错误 from、未知 target、跳级迁移和降级全部拒绝。

### CN-001：Notion unknown recovery 可重复创建 page

自定义 `children` payload 不保证嵌入双 marker。远程写成功但本地结果 unknown 后，marker search miss，后续路径可能再次执行 create。

- 证据：`scripts/harness_db.py:4865`、`scripts/harness_db.py:5426`。
- 修复结果：marker 必须进入远程可搜索且不可被调用者覆盖的位置；ambiguous miss 不得自动 create。
- 测试：模拟 remote success/local failure，重试后的 page POST 总数必须为 1。

### CN-002：Linear comment/update 绕过 namespace scope

只要存在 Linear profile，comment/update 可直接使用任意 issue ID，未证明该 issue 属于绑定 team/project。

- 证据：`scripts/harness_db.py:4558`、`scripts/harness_db.py:4915`。
- 修复结果：执行前读取 issue metadata 并核对 team/project；无法确认时 fail-closed。
- 测试：两个项目共用账号时，跨 project issue comment/update 必须零写入。

### CN-003：Idempotency key 不绑定 payload

相同 idempotency key 的 re-plan 可覆盖已 completed action 的 payload，同时保留 completed/external ID，形成“新请求继承旧成功”的 false completion。

- 证据：`scripts/harness_db.py:5387-5413`。
- 修复结果：持久化 canonical payload hash；同 key 不同 payload 永远冲突；completed action immutable。
- 测试：相同 key/相同 payload复用；相同 key/不同 payload冲突；completed 不可被 re-plan 改写。

### IN-002：Project doctor 给出不可执行命令

`kafa project doctor` 声称普通业务项目不需要 plugin source，却输出业务项目内的 `plugins/codex-project-harness/scripts/harness.py` 路径；空项目执行失败。`command_project()` 在 report unhealthy 时仍返回 0。

- 证据：`kafa/cli.py:119-131`、`kafa/cli.py:301-336`。
- 修复结果：提供 location-independent launcher，例如 `kafa project init/status/quickstart` 或已安装 skill runtime launcher；unhealthy 返回非零。

### IN-003：Hooks 安装路径和版本读取错误

Hook fallback 假定业务 repo vendored `plugins/codex-project-harness`；标准 user install 并不满足。测试始终注入 `CODEX_PROJECT_HARNESS_PLUGIN_ROOT`，没有覆盖真实 fallback。Hook 又从 `../../VERSION` 读取版本，复制后的 plugin 没有该文件，因此显示 `version: unknown`。

- 证据：`hooks/hooks.json:9-60`、`hooks/harness_hook.py:94-97`、`tests/test_codex_hooks.py:150-160`。
- 修复结果：hook 从自身 plugin manifest 读取版本，命令在缓存安装、user install 和 repo-local install 中都可解析。

### IN-004：Doctor 和 CI 不验证真实安装

`kafa doctor` 无条件把 marketplace path 标为 OK，不检查文件、resolved source、Codex plugin list 或 hook execution。CI 只 editable-install Python package 和 source doctor。

- 证据：`kafa/cli.py:271-298`、`.github/workflows/validate.yml:55-89`。
- 修复结果：增加 isolated-HOME install smoke；doctor 区分 source health、marketplace health、installed plugin health、runtime health。

### RL-001：发行事实分叉

仓库和 changelog 声明 `1.25.0-beta.1`，GitHub 最新 tag/release 是 `1.21.3-beta.1`，相差六个提交。没有 release workflow，也没有可验证的 root marketplace distribution。

- 影响：用户无法确定安装来源；源码、部署 clone、installed plugin 和 cache 可能各自不同。
- 修复结果：tag、manifest、VERSION、package、release notes 和 install smoke 由一个 release manifest 驱动。

### EV-001：`live-codex` 永远 skipped

即使设置 enable flag 且 Codex CLI 可用，代码仍返回“no repository-local live profile configured”。`should_fail()` 把 live skipped 当成功。

- 证据：`scripts/run_agent_e2e_eval.py:1114-1155`。
- 修复结果：实现真实 native task/subagent/worktree/collect/verify 流程；显式请求 live 时，未配置应是 not-run/blocked，不得计为 capability pass。

### EV-002：Fixture success 绕过 delivery validation

`parallel_success` 暂时 monkeypatch `validate_runtime = lambda: []` 后执行 integrate，因此成功场景没有验证真实 gate。

- 证据：`scripts/run_agent_e2e_eval.py:529-536`。
- 修复结果：fixture 必须通过真实 public interface 和真实 gate；禁止 monkeypatch release-critical decision。

### HP-001：Host Provider 生命周期不可靠

- timeout 只被记录，没有实际 watchdog；
- status/heartbeat 接近 no-op；
- collect polling 会续租已经死亡的 worker；
- cancel 只 SIGTERM Python worker，不能确认 SDK app-server/turn 已终止。

证据：`core/agent_provider.py:314`、`core/agent_provider.py:428`、`core/agent_provider.py:455`、`scripts/harness_db.py:7104`。

修复结果：在 native-first 迁移前，legacy Host Provider 必须有真实 process tree cancellation、deadline、liveness probe 和 terminal-state CAS。

### HP-002：Host Provider 绕过父任务权限

Worker 固定使用 `Sandbox.workspace_write` 与 `ApprovalMode.deny_all`，不继承 ChatGPT/Codex 当前 task 的权限、approval 和 workspace policy。

- 证据：`core/agent_provider.py:486-495`。
- 修复结果：native task 权限为权威；不能继承时 fail-closed，不能静默改为更宽或不同权限。

### HP-003：Host Provider 不是原生 subagent 集成

安装的 role TOML 没有被 Host Provider 选择；Provider 启动 generic standalone SDK thread，记录固定 `sdk-turn`，不支持 native resume、steer、interrupt、fork、handoff、archive，也不出现在用户可见 subagent UI 中。

- 证据：`scripts/harness_db.py:2182`、`core/agent_provider.py:510-518`。
- 修复结果：Codex/ChatGPT 负责 thread 和 subagent orchestration；Kafa 只接收 host task/thread/worktree receipt 并验证结果。

### ST-001：本地主事实源无法跨 managed worktree/cloud

`.ai-team/state/` 和 `.ai-team/runtime/` 被强制 gitignore。Codex managed worktree、handoff 和 ChatGPT Work hosted task 不会自然获得 SQLite 主事实源。复制活动 SQLite 到多个 worktree 又会制造分叉。

- 证据：`scripts/harness_db.py:76-82`、`scripts/harness_db.py:1225-1266`。
- 修复结果：必须明确选择：产品严格 local-only，或提供由根工作区托管的 Project Fact Transport。禁止每个 worktree 独立复制并修改数据库。

### PK-001：安装器强制携带 Host SDK

根 package 仅提供轻量安装 CLI，却 mandatory 依赖 `openai-codex>=0.1.0b3`。CI 每个平台额外下载约 84-95 MB 的 Codex CLI binary 和 Pydantic 依赖。

- 证据：`pyproject.toml:16-18` 和 CI install log。
- 修复结果：基础安装保持轻量；Host SDK 作为 optional extra 或 legacy provider 专用安装。

## P2：架构和产品演进问题

### HP-004：`manual-csv` 是不完整 Provider

Provider `collect()` 永远返回 `None`；真实 CSV import 是另一条不闭合 provider session 的流程。

- 证据：`core/agent_provider.py:129`、`scripts/harness_db.py:6967`。
- 处理：删除伪 Provider 命名，或把 export/import 正式建模为同一 lifecycle。

### AR-001：Kernel module 只有名义 seam

`harness_db.py` 约 8,437 行、298 个顶层函数；`core.api` 动态 re-export 所有非私有符号，`gate_engine` 又反向 import `harness_db`。interface 几乎等于 implementation。

- 证据：`core/api.py:13-25`、`core/gate_engine.py:193`。
- 处理：形成 Delivery Decision、Cycle Ledger、Schema Lifecycle、Connector Governance、Native Runtime Adapter 等 deep modules。

### AR-002：Feature freeze 冻结了错误对象

当前测试固定完整 table、CLI、skill、schema、core/script/hook 文件集合。它能发现意外扩张，却也阻止通过新增内部 module 深化架构。

- 证据：`tests/test_feature_freeze.py:236-264`、`scripts/validate_structure.py:12-119`。
- 处理：冻结 public interface、migration compatibility、trust invariants 和 release contract；内部文件允许重构。

### AP-001：Connector 重复实现 ChatGPT Apps/MCP

Plugin manifest 只声明 skills，没有 apps/MCP；Kafa 直接实现 GitHub CLI 和 Linear/Notion/Figma/Slack bearer-token HTTP。

- 证据：`.codex-plugin/plugin.json:20`、`scripts/harness_db.py:4631`、`scripts/harness_db.py:4892`。
- 处理：ChatGPT Apps/MCP 负责 OAuth、workspace policy、tool approval 和外部动作；Kafa 只治理 project scope、idempotency intent、receipt 和 fallback。

### MR-001：模型策略与宿主能力演进脱节

当前路线只区分 Spark 与 SDK default，并硬编码 research-preview Spark slug。最新 Codex/ChatGPT 已支持宿主原生 subagent model/reasoning/sandbox/MCP/skills 配置和动态路由。

- 处理：Kafa 输出 capability/risk hints，不决定具体模型 slug；最终选择由 host policy 执行并记录 receipt。

### DC-001：文档和版本说明漂移

README 顶部声明 v1.25，但尾部仍称当前 README 描述 v1.20 / Kernel 4.13。安装文档同时出现源码 repo 路径、业务项目 vendored 路径和 installed skill launcher，权威入口不唯一。

- 证据：`README.md:648-656`、`QUICKSTART.md:17-39`。
- 处理：按用户旅程重写文档；版本流水账迁移到 CHANGELOG，README 只保留当前行为。

## ChatGPT + Codex 融合后的目标边界

最新 Codex/ChatGPT 已原生提供：

- 用户可见的 subagent threads 与并行 orchestration；
- custom agents 的 model、reasoning、sandbox、MCP 和 skills 配置；
- managed worktree、handoff、任务恢复和审批；
- Plugin 内的 skills、hooks、Apps 和 MCP；
- connected sources、workspace policy 和 scheduled tasks。

因此 Kafa 的长期目标不应是另一套 Agent 平台，而应收束为：

```text
Skill / AGENTS
  -> 自然语言入口与持久方法约束

Native Codex / ChatGPT
  -> task、thread、subagent、worktree、handoff、approval、model routing

ChatGPT Apps / MCP
  -> 外部授权、读取、写入和工具级审批

Kafa Kernel
  -> requirement、acceptance、cycle、candidate、validation、finding、delivery decision

Compatibility Evals
  -> 证明真实宿主组合可运行，而不仅是 fake fixture 可运行
```

Kafa 应继续保留的差异化能力：

- candidate-scoped delivery decision；
- requirement/acceptance/failure-mode traceability；
- controller-side semantic test evidence；
- structure-derived blocker 和 risk acceptance；
- 外部写入的 project scope 与 intent audit；
- 不把 worker、connector 或 eval output 直接当 delivery evidence。

## 修复顺序

### Wave 0：冻结错误扩散并建立失败基线

**目标**：让每个已复现缺陷先成为 failing regression test。

1. 暂停新版本 tag、Connector feature、Provider feature 和 schema feature。
2. 为 DT-001、DT-002、CY-001、TR-001、QS-001、IN-001 添加红测。
3. 为 DB-001/002、CN-001/002/003 添加最小复现测试。
4. 在 issue 文档中记录每个测试的旧行为和目标行为。
5. CI 增加 `known-failure` 审计，避免修复过程中遗漏任一缺陷。

**退出条件**：所有问题都有稳定、平台无关的失败测试；尚未声称问题修复。

### Wave 1：先修 Schema Lifecycle

**目标**：为 cycle identity、gate sequence 和 legacy trust migration 提供可靠基础。

1. 修复 `executescript()` 事务语义。
2. 引入显式 migration registry 和 actual-version CAS。
3. 设计 schema 29：cycle-scoped identity、quality gate sequence/supersede、legacy trust status。
4. 完成 schema 28 -> 29 dry-run、backup、apply、rollback 测试。

**退出条件**：迁移失败不会留下半迁移状态；未知版本不可写入。

### Wave 2：修复 Delivery Truth 与 Trust Root

**目标**：关闭所有可能导致 false delivery 的 P0。

推荐顺序：

1. DT-002：建立确定的 gate 全序；
2. DT-001：从结构化 findings 派生 blockers；
3. CY-001：迁移 cycle-scoped identity 和所有 same-cycle query；
4. TR-001：分离 token issuer 与 verifier，降级 legacy self-signed facts；
5. QS-001：quickstart 不再自动 fresh QA/delivered。

**退出条件**：对抗测试无法通过伪造 origin、旧 pass、未解决 finding、跨 cycle row 或同进程 quickstart 获得 delivery-ready。

### Wave 3：恢复安装和发行可信度

**目标**：让全新用户按一个入口完成安装和业务项目初始化。

1. 修 IN-001 user marketplace source。
2. 提供稳定的 `kafa project init/status/quickstart` launcher。
3. 修 hooks 的 installed path 和 manifest version 读取。
4. doctor 分层检查 source、marketplace、installed plugin 和 business runtime。
5. 将 Host SDK 从基础安装依赖移出。
6. 建立 isolated-HOME install/upgrade/uninstall CI。
7. 建立 release manifest、tag 和 GitHub prerelease workflow。

**退出条件**：全新 macOS/Linux/Windows 环境不需要源码绝对路径或手工 marketplace 修正。

### Wave 4：修复 Connector 数据安全

**目标**：恢复 namespace 和 exactly-once 声明的真实性。

推荐顺序：

1. CN-003：先让 idempotency key 绑定 immutable payload hash；
2. CN-002：所有 object update/comment 先远程确认归属 scope；
3. CN-001：marker placement 与 ambiguous outcome 状态机 fail-closed；
4. 对五个 connector 做跨项目、超时后写、权限不足、marker miss 的对抗矩阵；
5. 制定 Apps/MCP receipt adapter 迁移 ADR，direct HTTP 进入 legacy mode。

**退出条件**：并发、崩溃、超时和跨项目场景都不会重复写或越权写。

### Wave 5：Codex/ChatGPT 原生化

**目标**：停止维护第二套不可见 agent orchestration。

1. 写 ADR：Native Codex owns task/thread/subagent/worktree/approval lifecycle。
2. Kafa dispatch 改为生成任务约束和接收 native receipt，而非启动通用 SDK worker。
3. 记录真实 host task/thread/worktree IDs，支持用户可见跳转和审计。
4. 明确 local-only 与 ChatGPT Work hosted 的 state transport 策略。
5. Host SDK 降为 optional legacy/noninteractive adapter。
6. 删除或闭合 `manual-csv` fake provider。
7. 模型策略改为 capability/risk hint，不硬编码具体默认模型。

**退出条件**：原生 subagent、worktree、approval、cancel、steer 和 handoff 的状态由宿主负责；Kafa 不再伪造 lifecycle。

### Wave 6：建立真实 Compatibility Evals 与发布门

**目标**：CI 绿色能够代表用户路径真实可用。

1. 移除 fixture 对 `validate_runtime` 的 monkeypatch。
2. 实现 real local Codex task/subagent/worktree E2E。
3. 实现 plugin install + hook + skill discovery E2E。
4. 增加 Apps/MCP receipt contract E2E。
5. 显式 live profile 未配置时返回 not-run/blocked；不得计为 capability pass。
6. 发布前校验 tag、manifest、VERSION、package、marketplace 和 installed plugin 一致。

**退出条件**：release gate 同时包含 deterministic kernel matrix 和至少一个真实宿主 compatibility profile。

### Wave 7：架构深化与文档收束

**目标**：让后续修复集中在稳定 module 内，不再继续放大 `harness_db.py`。

建议 deep modules：

- `Delivery Decision`：candidate snapshot -> deterministic decision；
- `Cycle Ledger`：cycle-scoped immutable facts 和 traceability；
- `Schema Lifecycle`：open/current/migrate/restore；
- `Attestation Verifier`：只验证外部 receipt；
- `Connector Governance`：scope、intent、payload hash、receipt、fallback；
- `Native Runtime Adapter`：宿主 thread/task/worktree receipt；
- `Release Truth`：版本、marketplace、安装和 compatibility matrix。

同时把 feature freeze 从“文件和表不能变化”改为：

- public CLI compatibility；
- schema migration compatibility；
- delivery/trust invariants；
- plugin manifest contract；
- release installation contract。

## 目标模式的推荐拆分

不要创建一个覆盖全部问题的超大目标。建议依次开启以下目标：

1. `Goal A - Stop-ship regression baseline`
2. `Goal B - Schema 29 and deterministic delivery decision`
3. `Goal C - External attestation authority and quickstart trust repair`
4. `Goal D - Installation and release recovery`
5. `Goal E - Connector safety repair`
6. `Goal F - Native Codex/ChatGPT alignment`
7. `Goal G - Real compatibility evals and kernel deepening`

每个目标都必须完成：红测、最小实现、migration/compatibility 验证、对抗式审查、完整回归和独立质量门。前一目标未通过，不进入后一目标。

## 仍需 ADR 决策的问题

以下问题不能在实现中临时猜测，进入对应目标前应形成 ADR：

1. 外部 attestation issuer 采用本地 broker、CI identity、OS keychain 还是远程 verifier；
2. project fact state 是否严格 local-only，还是支持 host-managed transport；
3. Native Codex receipt 能提供哪些稳定 task/thread/worktree identity；
4. ChatGPT Apps/MCP receipt 的可验证字段和权限语义；
5. schema 29 的 cycle identity 与 gate sequence 迁移策略；
6. legacy direct Connector 和 Host SDK 的弃用周期。

## 验证原则

后续任何修复不得只通过源码字符串、mock 成功或手填角色证明完成。最低验证要求：

- public CLI/interface 行为测试；
- SQLite failure injection 和 rollback 测试；
- 相同时间、并发、重试、崩溃、跨 cycle、跨 project 的对抗测试；
- isolated HOME 的真实 Codex plugin 安装测试；
- native Codex live profile；
- delivery gate 的 negative tests 多于 happy-path tests；
- skipped、unknown、degraded、audit-only 永远不能计为 capability pass 或 delivery pass。

## 当前证据状态

- 仓库审计前后均为 clean，`main` 与 `origin/main` 同步。
- 最新 GitHub Validate workflow 为绿色。
- 绿色 CI 不否定本文问题：现有测试明确固化了部分错误路径，真实 live Codex 又永久 skipped。
- 审计过程中已在临时目录复现 HMAC 自签、critical finding 漏阻断、latest gate 误选、cycle 历史搬迁、migration 任意版本、Notion 重复写、Linear scope 绕过和 user install 失败。
- 本文没有修改 runtime、schema、CLI、Plugin 或测试行为。


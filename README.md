# Codex Project Harness

Codex Project Harness（Kafa）是一个面向 Codex 的 **local-only verified delivery kernel**。它把需求、验收、任务、验证、审查和代码交付事实保存在项目本地，用可复验的执行证据回答一个核心问题：当前本地 candidate 是否已经具备可信的代码交付条件。

Kafa 不替代 Codex/ChatGPT，也不实现第二套协作生命周期。Native Codex/ChatGPT 是 task、subagent、worktree、approval、model、cancel 和 handoff 的唯一 owner；Kafa 只在根工作区维护交付事实、验证当前 candidate，并给出诚实的 delivery decision。

当前源码候选版本是 **v2.0.0-beta.1**，`release.json` 将其明确标记为 `development`，因此它不是已发布版本。最新正式 tag/release 以 GitHub 和 `git tag` 为准。当前架构代际定位为 **Codex Harness Kernel v5.0.0**。它只负责交付经过验证的代码和证据，不负责生产部署、上线发布、基础设施开通、生产迁移、密钥变更或付费资源创建。

## 三个权威边界

一次完整交付只有三个清晰的 owner：

| 层 | 权威内容 | 不负责 |
| --- | --- | --- |
| OpenSpec | 在需求不清晰、中大型功能、架构或跨模块变更中，维护 proposal、design、tasks 和归档后的产品行为 | 不保存 Kafa 的运行时事实 |
| Kafa | 本地 SQLite 中的需求、验收、任务、不可变执行、验证判断、finding、质量门和交付结论 | 不创建或管理 Native Codex/ChatGPT 的协作生命周期 |
| Native Codex/ChatGPT | task、subagent、worktree、approval、model、cancel、steer 和 handoff | 不直接写 Kafa SQLite，也不把自报文本升级为验证证据 |

OpenSpec 是需要规格化时的 spec authority；Kafa 是 verified delivery authority。Kafa 可以引用 OpenSpec 的路径和结论，但不会复制一套 OpenSpec 文档作为自己的事实源。

## 一条本地交付路径

```text
User intent
  -> OpenSpec proposal/design/tasks（需要时）
  -> Kafa init + local requirement baseline
  -> root controller 建立 task 与 test target
  -> Native Codex/ChatGPT 完成可见的本地代码工作
  -> root controller 接收结果并推进 task
  -> Kafa verify run 独立执行当前 candidate
  -> validation + finding + independent quality gate
  -> delivery decision / verified code handoff
```

这条路径有四个不可绕过的约束：

- 业务运行时只使用项目文件、本地 Git 或内容身份、项目级 SQLite，以及可选的本地容器执行；不需要外部凭证，也不直接调用项目管理 SaaS API。
- 只有根控制器可以修改 Kafa 事实。子任务执行者返回代码、审查信息和上下文标识，由根控制器验证和记录。
- 命令证据只能由 controller executor 生成，并以 immutable execution 保存。人工文字只能是判断或审计说明。
- High/critical 风险没有可验证 provenance 时返回 `human-review-required`；它不是通过状态。

## 快速开始

从 Kafa 源码仓库安装 repo-scoped plugin：

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

重启 Codex 后，从 `kafa-local` marketplace 安装 `codex-project-harness`。在普通业务项目中初始化：

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . init
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . status
```

如果目标已经清晰并且有真实测试命令，可以运行最小闭环：

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . quickstart minimal \
  --id SMOKE \
  --goal "Keep the current behavior working" \
  --acceptance "The existing test command passes" \
  --task "Verify the current behavior" \
  --test-command "python3 -B -m unittest discover -s . -p 'test_*.py'" \
  --execute
```

`--execute` 会通过 controller executor 运行已登记的 target。退出码为零但没有有效结构化结果、通过数为零、结果文件缺失或结果语义失败，都不能生成 passing validation。

更完整的操作顺序见 [QUICKSTART.md](QUICKSTART.md)，安装、升级和 schema 迁移见 [INSTALL.md](INSTALL.md)。

## OpenSpec 与 Kafa 的分工

当需求不清晰、变更跨模块、影响长期行为或需要架构决策时，先在 OpenSpec 中锁定提案、设计与任务：

```bash
openspec status --change <change-name>
openspec validate <change-name>
```

实施时以该 change 的 `tasks.md` 为清单，并在完成后更新 checkbox。Kafa 记录与交付相关的最小本地事实：需求和验收链接、task 状态、当前 candidate 的执行、验证判断、finding、质量门和最终交付。小型且验收明确的安全改动可以直接使用对应的 Kafa Skill，不强制创建 OpenSpec change。

## 根控制器单写

Kafa 的 task 状态机是：

```text
planned -> active -> submitted -> accepted
                    |            -> blocked
                    -> blocked

planned / active / submitted -> cancelled
```

公开 task 操作只有：

```text
task add
task list
task start
task submit
task accept
task block
task cancel
```

状态转换必须按顺序发生；例如 planned 或 active task 不能直接 accept。`revision` 只作为审计序号。Native Codex/ChatGPT 完成本地工作后，根控制器检查实际 diff 和 candidate，再执行 `task submit`、验证和 `task accept`。任何子任务执行者都不应调用 Kafa mutation 命令。

## 不可变执行与验证判断

Runtime 使用 schema 30 的 27 张 local-core tables。主事实源是：

```text
.ai-team/state/harness.db
```

其中：

- `executions` 保存当前 candidate、target、命令、真实退出码、输出 digest、artifact、执行计数、结构化结果和 sandbox policy；记录后不可覆盖。
- `validations` 保存对验收面和风险面的判断，并通过关系表引用 execution；它不能复制或修改命令事实。
- `events` 是 compact append-only audit log，只记录受影响实体和有界摘要，不是数据库恢复或 replay 来源。
- `.ai-team/` 与 `docs/harness/` 下的 Markdown 是按影响范围更新的阅读视图，不是第二事实源。

登记和执行本地 target 的典型命令：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . test-target add \
  --id UNIT \
  --kind unit \
  --command-template "python3 -B -m unittest discover -s tests -p 'test_*.py'"

python3 plugins/codex-project-harness/scripts/harness.py --root . test-target link \
  --task T1 \
  --target UNIT

python3 plugins/codex-project-harness/scripts/harness.py --root . verify run \
  --target UNIT \
  --acceptance AC1
```

需要本地隔离执行时，使用 `verify run --runner container --container-image <image>`。如果 target 声明必须 sandbox 或 no-network，实际执行 metadata 不满足时会 fail closed。

`validation record` 只记录判断。没有 controller execution 支撑的自由文本，不会变成 delivery gate 可接受的命令证据。

如果 structured runner 通过 `--result-path` 生成结果文件，建议把路径放在
`.ai-team/runtime/` 下，或直接从 stdout 解析。命令若在普通项目路径创建或修改结果
文件，post-execution candidate 会正确变化，该次 execution 将以 stale candidate
丢弃；Kafa 不会为了结果文件而动态排除任意业务源码路径。

## Canonical project path safety

Kafa 自己管理的 DB、lock、sentinel、projection、template 和 execution artifact
只接受 pinned project root 下的普通单链接对象。一个 root-level symlink alias 会在
操作开始时解析一次；root 以下的 symlink、junction、reparse point、hard link、
非普通文件或跨设备 ancestor 都会 fail closed。稳定错误格式为
`unsafe-project-path: <relative>: <reason>`，完整 reason 与恢复步骤见
[INSTALL.md](INSTALL.md#canonical-project-path-safety)。Kafa never automatically
follows, rewrites, deletes, or repairs an unsafe link。

Python 标准库 SQLite 只能接收 pathname，因此 Kafa 使用已验证且禁止隐式创建的 URI，
并在 connect、journal setup 和 close 边界复核 identity；有限替换会关闭连接并返回
`path-identity-changed`，不会被报告成功。持续拥有同一 OS user 权限的主动攻击者不在
保证范围内；这类仓库应放入 isolated OS user or container。

Kafa does not sandbox arbitrary verification commands。只有显式选择现有 container
runner 时，才提供对应的本地 sandbox/no-network policy；ProjectFS 只保护 Kafa 自己
的 canonical artifact 操作。若不安全路径出现在迁移回滚中，保留 sentinel、manifest、
DB 和 projection backup，并按 `rollback-incomplete` 处理，不能自动移除链接或把失败
描述为完整回滚。

## Delivery trust

本地信任状态分为：

- `controller-verified`：根控制器针对当前 candidate 执行的 target。
- `reviewed-local`：不同 producer/reviewer context 的本地审查元数据。
- `same-context-degraded`：同一 context 的降级审查，只适用于 low/medium 风险。
- `human-review-required`：high/critical 默认结果，表示不能自主交付。

High/critical failure mode 至少需要当前 candidate 的 structured execution 和不同 producer/reviewer context。即使满足这两项，没有独立可验证 provenance 时仍必须返回 `human-review-required`。只有用户明确接受或豁免全部剩余高风险，并完整记录 actor、reason、范围、revision 和 expiry，才可以沿 accepted-risk 路径继续；该记录是程序性审计，不是密码学证明。

`skipped`、`blocked`、`not-run`、fixture-only 和零测试数都不能描述为通过。代码在 execution 或 quality gate 之后发生变化时，旧事实仍可审计，但不再满足当前 candidate。

Candidate identity 会纳入普通 ignored runtime source，但把未版本化且精确命名的
top-level dependency/tool environment（`.venv/`、`venv/`、`.tox/`、
`.nox/`、`node_modules/`）与生成工具缓存排除在源码身份之外。任何位于这些根目录
下的 Git versioned path 都会让整个根目录重新进入严格源码扫描；`.venvish/` 等相邻
前缀不会被排除。项目 lockfile 和 dependency manifest 始终参与 candidate identity。
在保留的项目路径中，只有 exact generated projection、retired projection 和三个静态
agent template 会被排除；`.gitignore`、额外的 `.codex/agents/` 或
`docs/harness/` runtime 文件仍属于 candidate。No-Git 项目遇到 FIFO、socket 或
其他非普通路径会 fail closed。Git 模式会分别检查 index 与 HEAD，因此即使 gitlink
只存在于 HEAD、删除已暂存且 worktree 路径不存在，也不能生成可用 candidate。
Identity Git 命令同时禁用 replace-object lookup；仓库内的 `refs/replace/*`
不能用替代 commit/tree/blob 隐藏真实 HEAD gitlink 或缺失对象。
受控 `GIT_WORK_TREE` 同时固定实际评估根目录，repo-local `core.worktree`
不能重定向 source inventory 或触发 Git 到 content identity 的静默降级。

持久 Native 报告只接受四个精确 mode，并绑定 `evidence_scope`、matrix profile、
有序 scenario inventory、local scenario category/mode、正数且有限的 token/runtime/
duration，以及 parallel task-to-scope/context/target/acceptance 映射。生成时还会核对
当前 Native Codex binary 与 CLI version；未知 Connector/Host 标签、零 telemetry、
producer 互换或自洽但错误的 binary metadata 都不能保留 passing 状态。
Passing matrix 必须同时记录 Codex available 且无 skip reason，成功 producer 的
error 必须为空，Git dirty/status count 必须一致；Host 未暴露可信金额时
`estimated_cost` 固定为 `null`。生成时 platform、Python、Git 和 container facts
必须与当前环境一致；跨平台读取持久报告时只把它们作为经过类型校验的历史事实，
不伪称是在当前机器生成。
Compact evidence 使用 closed `report_version=1`；passing live detail/producer
出现未声明字段会失败，显式 test binary override 只能用于 evaluator 回归，不能通过
`--evidence-out` 写成 persistent real-Native evidence。evaluation-scoped Git
source 只要存在 unmerged entry，也不会生成可用 workspace digest。版本、summary
counter、return code、execution/validation/workload/producer count 与 token count
均使用递归 exact JSON 类型校验，`true`/`false` 或浮点数不能冒充整数。
Fixture/stability 的每个 detail counter 在聚合前必须是 exact non-negative
integer；passing single/parallel 还要求 active table inventory 精确等于 schema 30
的 27 张表，并且 catalog 只额外允许 `sqlite_sequence`；任何其他 `sqlite_*`
或 Connector/Host/runtime 表都会 fail closed。Real Native controller command 从
启动身份验证过的 private Git-backed snapshot 执行，报告绑定 start identity 并复核
completion identity，短暂替换后恢复的源码也不会成为实际执行字节。

## Public CLI

统一 CLI 只公开以下顶层领域：

```text
init  status  doctor  quickstart
cycle  requirement  acceptance  failure-mode  baseline  trace
task  test-target  verify  validation
finding  gate  delivery  decision
validate  repair  migrate  projection
```

先用 `--help` 确认具体参数：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --help
python3 plugins/codex-project-harness/scripts/harness.py task --help
python3 plugins/codex-project-harness/scripts/harness.py verify run --help
```

`projection rebuild` 是本地视图恢复命令。正常 mutation 只重建受影响的视图。`repair` 在修改前创建并验证 SQLite backup。

## Plugin surface

Plugin 保留七个 delivery-focused Skills：

| Skill | 用途 |
| --- | --- |
| `project-harness` | 从工作区检查、OpenSpec 路由和需求基线到 verified handoff 的总入口 |
| `minimal-safe-change` | 验收明确的小型安全改动 |
| `bug-fix-loop` | 复现、定位、修复和回归 bug |
| `test-first-delivery` | 契约敏感或回归敏感的测试优先交付 |
| `independent-quality-gate` | 独立 QA、finding 和 current-candidate 审查 |
| `harness-audit` | 审计运行时、边界和交付证据 |
| `project-retrospective` | 交付后复盘和方法改进 |

Plugin 只定义三个 Hooks：

- `SessionStart`：只读注入本地状态。
- `SubagentStart`：注入根控制器单写和角色边界。
- `Stop`：仅给出警告，不阻止 Native Codex/ChatGPT 停止。

未初始化项目中的 Hook 会简洁跳过，不创建 `.ai-team`。项目初始化只安装三个静态 Native Codex agent templates：`developer.toml`、`architect.toml` 和 `qa-reviewer.toml`；模板提供角色说明，但 Kafa 不拥有其生命周期。

## Schema 30 migration and recovery

支持的 v1 schema 迁移通过 side-by-side conversion 完成：先创建带 digest 和完整性结果的 SQLite backup，再把有效本地事实复制到 staging schema 30，验证 foreign keys、invariants 和 projection dry-run，最后原子替换 active DB。

被移除的远程协作、执行者生命周期和历史恢复子系统数据只保留在 pre-migration backup，不会进入 active schema 30。激活后 doctor 失败时，运行时使用已验证 backup 自动恢复；schema 30 写入新事实后不承诺自动 downgrade。

迁移在原子替换前会持久化 `recovery-required` sentinel 和 manifest 路径。只有迁移成功或 DB 与 projections 都达到 verified complete rollback 后才会清除 sentinel；`rollback-incomplete`、hard process exit 或 recovery interruption 会保留它并阻止普通命令。Operator must not remove 该 sentinel，直到根据 manifest 恢复并验证 database/projection authority。任何缺少 mandatory projection activation validator 的 core migration 调用都会在激活前被拒绝。

所有 production projection publication（包括 `projection rebuild`、same-schema
migrate、repair 和普通 mutation）从 DB read 到最终文件写入的完整生命周期持有同一个
operation lock。迁移成功前会在私有 DB snapshot 中独立渲染 13 个 view，并逐字节比较
live projection；仅有文件存在不等于验证通过。`project-state.yaml` 使用 SQLite
`project.updated_at`，不使用 render-time clock，并在 rebuild 时 replace rather than merge，
字段集合严格匹配 schema（含 DB `id/current_cycle_id`，不伪造 `blocked_reason`），
因此相同 DB 产生相同字节且陈旧附加键不会残留。失败 schema-30 的 WAL/SHM 必须先隔离，
恢复后的 source DB 再通过普通 SQLite read-only 语义验证；无法隔离 handle/sidecar 时
保持 `rollback-incomplete`。即使 active DB 缺失，status/doctor/validate/quickstart
status 也会先显示 recovery manifest 和 do-not-remove guidance，而不会建议重新 init。
Core 会在 callback 返回后自行比较 projection bytes；callback self-report 不能成为成功
依据。Callback 前后 active DB fingerprint 也必须不变，即使注入值能通过 doctor 也会
rollback。Operation lock 的 descriptor/open/unlock cleanup 对 `BaseException` 安全。

具体 dry-run、backup 路径和恢复边界见 [INSTALL.md](INSTALL.md)。

## 交付边界

Kafa 会：

- 形成本地需求、验收、failure mode 和 task traceability。
- 保存当前 candidate 的 immutable execution 和 validation judgment。
- 记录 finding、quality gate、remaining risk 和 delivery decision。
- 输出 verified code handoff 所需的证据和明确的未验证范围。

Kafa 不会：

- 代替 Native Codex/ChatGPT 管理 task、subagent、worktree、approval、model、cancel 或 handoff。
- 直接操作业务项目的远程协作系统。
- 把自报上下文标识、人工文字或 Hook 输出伪装成可信执行证据。
- 自动 commit、push、merge、tag、release、deploy 或执行生产变更。

## 维护本仓库

结构和本地回归入口：

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
python3 -m py_compile kafa/*.py plugins/codex-project-harness/scripts/*.py \
  plugins/codex-project-harness/core/*.py plugins/codex-project-harness/hooks/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/run_isolated_install_smoke.py --repo .
python3 plugins/codex-project-harness/scripts/run_runtime_smoke.py
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode fixture
python3 plugins/codex-project-harness/scripts/run_agent_e2e_eval.py --mode stability
git diff --check
```

真实 Native Codex compatibility profile 是显式 opt-in 的独立验证面。没有运行、能力不可用、认证缺失或场景被阻塞时，必须如实报告，不能用本地 fixture 结果替代。

版本变化记录见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT

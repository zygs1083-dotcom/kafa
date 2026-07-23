# Codex Project Harness

Codex Project Harness（Kafa）是一个面向 Codex 的 **local-only verified delivery kernel**。它把需求、验收、任务、验证、审查和代码交付事实保存在项目本地，用可复验的执行证据回答一个核心问题：当前本地 candidate 是否已经具备可信的代码交付条件。

Kafa 不替代 Codex/ChatGPT，也不实现第二套协作生命周期。Native Codex/ChatGPT 是 task、subagent、worktree、approval、model、cancel 和 handoff 的唯一 owner；Kafa 只在根工作区维护交付事实、验证当前 candidate，并给出诚实的 delivery decision。

当前源码候选版本是 **v2.0.0-beta.1**，`release.json` 将其明确标记为 `development`，因此它不是已发布版本。最新正式 tag/release 以 GitHub 和 `git tag` 为准。当前架构代际定位为 **Codex Harness Kernel v5.0.0**。它只负责交付经过验证的代码和证据，不负责生产部署、上线发布、基础设施开通、生产迁移、密钥变更或付费资源创建。

<!-- BEGIN GENERATED: workflow-contract:workflow-overview -->
## Workflow Authority

| Authority | Owns | Does not own |
| --- | --- | --- |
| OpenSpec | proposal, design, specs, and the unique tasks checklist for specification-led changes | Kafa SQLite runtime facts |
| Kafa SQLite | local delivery facts and immutable execution, validation, review, and delivery records | Native Codex/ChatGPT lifecycle |
| core.delivery.evaluate_delivery_prerequisites | executable fail-closed readiness, recording, and delivered-consistency decisions | documentation wording and workflow presentation |
| workflow-contract presentation source | generated owner, route, stage dependency, command, and advanced-trigger views | delivery eligibility and persisted project facts |
| Native Codex/ChatGPT | task, thread, subagent, worktree, approval, model, cancellation, steering, and handoff lifecycle | direct Kafa SQLite mutation |
| root controller | all Kafa fact mutation, candidate inspection, controller verification, integration, and delivery recording | invented independent provenance |

## Non-negotiable Safeguards

- `local-only`: Business runtime uses only project files, local Git or content identity, project SQLite, and optional already-local container execution.
- `root-controller-single-writer`: Only the root controller mutates Kafa facts; producers and reviewers return results through the Native Host.
- `native-host-lifecycle`: Native Codex/ChatGPT is the only owner of task, subagent, worktree, approval, model, cancel, steer, and handoff lifecycle.
- `immutable-execution`: Command evidence is created only by controller execution and is stored once without overwrite.
- `current-candidate-verification`: Execution, validation, qualification, gate, and delivery must remain current for the candidate under review.
- `fail-closed-delivery-gate`: Missing, stale, skipped, blocked, not-run, fixture-only, zero-count, or unverifiable evidence never becomes delivery pass.
<!-- END GENERATED: workflow-contract:workflow-overview -->

<!-- BEGIN GENERATED: workflow-contract:skill-routes -->
## Skill Routes

| Skill | Use when | Added obligation |
| --- | --- | --- |
| `project-harness` | broad, architectural, cross-module, long-lived, or complete verified delivery work | route to OpenSpec when specification is needed, then run the complete local delivery workflow |
| `minimal-safe-change` | small clear low-risk patch with explicit acceptance | keep the diff and evidence surface narrow |
| `bug-fix-loop` | reproducible defect or failing behavior | reproduce before fixing and retain a regression oracle |
| `test-first-delivery` | contract-sensitive or regression-sensitive behavior | establish the failing test before production change |
| `independent-quality-gate` | finished implementation needs fresh review | keep producer and reviewer contexts distinct when independent review is claimed |
| `harness-audit` | runtime, boundary, fact, or generated-view drift requires audit | audit evidence without relabelling missing checks as pass |
| `project-retrospective` | a completed milestone or repeated escape needs lessons captured | derive lessons from verified delivery evidence |
<!-- END GENERATED: workflow-contract:skill-routes -->

## 快速开始

从 Kafa 源码仓库安装 repo-scoped plugin：

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

重启 Codex 后，从 `kafa-local` marketplace 安装 `codex-project-harness`。在普通业务项目中初始化：

```bash
kafa project init --repo .
kafa project status --repo .
```

如果目标已经清晰并且有真实测试命令，可以运行最小闭环：

```bash
kafa project quickstart --repo . minimal \
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

Runtime 使用 active schema 31 的 30 张 local delivery tables。schema 30 仅作为固定兼容读取和 27/28/29/30 -> 31 迁移来源；主事实源是：

```text
.ai-team/state/harness.db
```

其中：

- `executions` 保存当前 candidate、target、命令、真实退出码、输出 digest、artifact、执行计数、结构化结果、sandbox policy，以及 schema 31 的 target/runtime/container provenance；记录后不可覆盖。
- `validations` 保存对验收面和风险面的判断，并通过关系表引用 execution；它不能复制或修改命令事实。
- `events` 是 compact append-only audit log，只记录受影响实体和有界摘要，不是数据库恢复或 replay 来源。
- `.ai-team/` 与 `docs/harness/` 下的 Markdown 是按影响范围更新的阅读视图，不是第二事实源。

登记和执行本地 target 的典型命令：

```bash
kafa project test-target --repo . add \
  --id UNIT \
  --kind unit \
  --command-template "python3 -B -m unittest discover -s tests -p 'test_*.py'"

kafa project baseline --repo . confirm \
  --id B1 --summary "Current candidate baseline" --by controller

kafa project test-target --repo . qualify \
  --id Q1 --acceptance AC1 --target UNIT \
  --rationale "UNIT directly proves AC1" --by controller

kafa project test-target --repo . link \
  --task T1 \
  --target UNIT

kafa project verify --repo . run \
  --target UNIT \
  --acceptance AC1
```

The generated workflow above is the maintained delivery order. A passing but
unqualified target, cancelled task, or missing readiness phase cannot cover an
acceptance. Historical cycles remain read-only through
`cycle audit --id <cycle-id> --json`; closed-cycle global IDs cannot be reused.

每个 schema 31 execution 在成为可交付证据前必须记录
`target_definition_sha256`、`platform`、`runtime_executable`、
`runtime_version`、`runtime_executable_sha256`、`policy_version` 和
`provenance_status=complete`。这些是可复核的本地执行环境事实，不是独立身份或
密码学 trust anchor；字段缺失、target/runtime 漂移或
`provenance_status=legacy-incomplete` 都不能满足当前 schema 31 delivery。

需要本地隔离执行时，使用 `verify run --runner container --container-image <image>`。
镜像必须已经存在于受支持的本机 Docker 或 Linux native-local Podman；Kafa 先记录
engine/version、冻结的 `container_engine_endpoint`、请求的镜像和
`container_image_digest`，再让全部 daemon 操作固定到该本地 endpoint，并用 immutable
identity、`--pull=never` 和受控 `/bin/sh` entrypoint 执行。Kafa 只读取该受控入口写入的
artifact，不把 container-engine CLI stdout 当作 target 结果。TCP/SSH/HTTP remote daemon、冲突的 Docker selector、
Podman remote/machine、隐式 pull、endpoint/engine/image 漂移、镜像不存在，或 target
声明的 sandbox/no-network metadata 不满足时都会 fail closed，且不创建 passing
execution/validation facts。

`validation record` 只记录判断。没有 controller execution 支撑的自由文本，不会变成 delivery gate 可接受的命令证据。

Structured result 建议写入 `.ai-team/runtime/` 或从 stdout 解析；业务源码路径中的
结果文件会改变 candidate，不能被动态排除。各 runner 必须提供可对账的 terminal
suite/package 结果；乱序、未终结、零测试、缺失或超限输出都 fail closed。声明
`result_path` 后缺失文件不会回退到 stdout。

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
- `same-context-degraded`：同一 context 的降级审查，只适用于 low/medium 风险，且 passing gate 必须记录非空 residual-risk。
- `human-review-required`：high/critical 默认结果，表示不能自主交付。

High/critical failure mode 至少需要当前 candidate 的 structured execution 和不同 producer/reviewer context。即使满足这两项，没有独立可验证 provenance 时仍必须返回 `human-review-required`。只有用户明确接受或豁免全部剩余高风险，并完整记录 actor、reason、范围、revision 和 expiry，才可以沿 accepted-risk 路径继续；该记录是程序性审计，不是密码学证明。

Medium failure mode 必须由与其 acceptance 显式关联的 qualified structured current-candidate execution 覆盖，或具备完整、当前、未过期的 accepted/exempt metadata。Open medium finding 同样阻断；完整接受的 medium 风险只进入程序性的 `accepted-risk`，不能豁免交付图、qualification、accepted task、candidate、execution 或 gate 前置条件。

Schema 31 的公开状态域是闭合契约：requirement 只能是 `active/cancelled`，acceptance 只能是 `active/cancelled`，failure mode 只能是 `identified/accepted/exempt`。CLI、写入 guard、SQLite DDL、doctor、migration、projection 和 JSON schema 共用这一权威集合；近似拼写、大小写或首尾空格不会被静默修正。仅 schema 30 的历史 failure-mode `active` 会在迁移时明确归一化为 `identified`。

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

真实 Native detail 使用闭合 contract，绑定 executable source、Git 状态、Native
binary、matrix、scenario、token/runtime 和 schema-31 table inventory。CI 将这类易变
detail 作为有期限 artifact 保存，默认 review surface 只显示绑定其精确字节 digest 的
stable summary。summary 不能脱离 detail 证明通过；缺失、digest 不符、stale、fixture、
显式测试 binary、dirty source 冒充 current，或 identity/matrix 不一致都会 fail closed。
仓库内 `docs/runtime/native-codex-*-summary.json` 明确把既有 dirty-worktree
明细标为 `historical`；后续候选运行默认在 CI/本地 opt-in 路径生成新明细，不靠改写
历史 bundle 制造 current 结论。
真实 controller 从私有 Git-backed snapshot 执行，并在结束时复核同一源码身份。

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
kafa project init --repo . --help
kafa project task --repo . --help
kafa project verify --repo . run --help
```

`projection rebuild` 是本地视图恢复命令。正常 mutation 只重建受影响的视图。`repair` 在修改前创建并验证 SQLite backup。

## Plugin surface

Plugin 保留上述七个 delivery-focused Skills；用途和路由由
`workflow-contract.json` 的生成视图统一维护。

Plugin 只定义三个 Hooks：

- `SessionStart`：只读注入本地状态。
- `SubagentStart`：注入根控制器单写和角色边界。
- `Stop`：仅给出警告，不阻止 Native Codex/ChatGPT 停止。

未初始化项目中的 Hook 会简洁跳过，不创建 `.ai-team`。项目初始化只安装三个静态 Native Codex agent templates：`developer.toml`、`architect.toml` 和 `qa-reviewer.toml`；模板提供角色说明，但 Kafa 不拥有其生命周期。

## Schema 31 migration and recovery

支持的 v1 schema 迁移通过 side-by-side conversion 完成：先创建带 digest 和完整性结果的 SQLite backup，再把有效本地事实复制到 staging schema 31，验证 foreign keys、invariants 和 projection dry-run，最后原子替换 active DB。schema 27、28、29 和 30 都是支持的迁移来源。

被移除的远程协作、执行者生命周期和历史恢复子系统数据只保留在 pre-migration backup，不会进入 active schema 31。激活后 doctor 失败时，运行时使用已验证 backup 自动恢复；schema 31 写入新事实后不承诺自动 downgrade。

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

以下内部脚本仅供 Kafa 源码维护者验证本仓库，不是普通业务项目的运行入口。结构和本地回归入口：

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

release workflow 先用完整 immutable Git OID 运行闭合 change-scope classifier。
Host、packaging、release-tooling、Native-evaluator 和 unknown 变更要求 blocking 的
single+parallel Native evidence；schema/runtime 与纯文档变更可为 advisory，但所有
确定性门禁仍运行。没有运行、能力不可用、认证缺失或场景被阻塞时必须如实报告，
不能用 fixture/stability 结果替代，也不能据此声称已发布。

版本变化记录见 [CHANGELOG.md](CHANGELOG.md)。

## License

[MIT License](LICENSE)

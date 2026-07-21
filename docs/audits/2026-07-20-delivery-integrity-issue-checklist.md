# Kafa delivery integrity 问题清单与修复验收基线

## 文档状态

- 状态：`P0-P2 local implementation closed / release activation not-run`
- 审计日期：2026-07-20
- 审计基线：`main@e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb`
- 远端对照：`main == origin/main`
- 源码版本：`2.0.0-beta.1 / runtime 5.0.0 / active schema 31`
- 审计方法：canonical spec 对照、源码路径审查、隔离临时项目确定性复现、GitHub 只读治理查询
- 本文用途：`delivery-integrity-hardening` 的唯一缺陷输入与逐项关闭清单；完成证明以对应 checkpoint 审计为准
- 变更边界：P0 production/runtime/schema 修复已完成；后续已单独授权并执行 commit/push/PR，普通 merge 待最终门禁；没有 tag、release、deploy、用户安装替换或业务项目数据库迁移

## 结论

四项 P0 均已被确定性红测证实，并已在 schema 31 runtime 中修复关闭。Kafa 现在要求最小可交付图、accepted task、显式 current qualification、共享 prerequisite evaluator 和合法 readiness；P0 checkpoint 的完整证据见 `2026-07-20-delivery-integrity-hardening-baseline.md`。

P0 修复关闭后仍然成立的解释边界：

- `delivery record` 或 `validate --delivery` 的成功只证明 canonical local kernel 前置条件，不等同于不可伪造的外部 provenance；
- procedural qualification 是显式、可审计的责任链，不是自然语言业务语义的自动证明；
- P2、完整最终回归和独立审查完成前仍不得据此发布新版本。

P1 中确认的 medium 风险策略冲突、closed state/schema、公开 journey 和 execution
provenance 问题均已按校准范围修复关闭。Schema 31 现在由 runtime、DDL、doctor、
projection 和 closed JSON-schema subset 共同约束；当前 P1 union 与独立复核证据见
`2026-07-20-delivery-integrity-hardening-baseline.md`。

P2 的三项本地实施和当前候选证据均已关闭。旧 live Codex 报告已被当前候选
single/parallel 报告替代，clean committed candidate evidence 已完成。默认分支
license detection 需在合并后由 GitHub 刷新；远端 attestation、tag、release 和 publish
仍未获授权并明确为 `not-run`，不被伪装成本地通过。

## 严重级别

| 级别 | 定义 | 发布策略 |
| --- | --- | --- |
| P0 | 可形成 false-delivery 或让底层 API 绕过 canonical delivery contract | 必须先修，stop-ship |
| P1 | 策略、schema、公开 workflow 或 provenance 契约不闭合 | P0 后第二批修复 |
| P2 | 当前证据、供应链或效果度量治理不足 | 不阻塞 P0 开发，但必须在正式 release 前有明确结论 |

## 问题总表

| ID | 级别 | 问题 | 核验结论 | 类型 |
| --- | --- | --- | --- | --- |
| KAFA-P0-1 | P0 | delivery 不要求最小可交付图 | 已修复并验证关闭 | runtime false-pass |
| KAFA-P0-2 | P0 | cancelled task 可充当完成覆盖 | 已修复并验证关闭 | runtime false-pass |
| KAFA-P0-3 | P0 | acceptance 与 target 缺少显式资格绑定 | 已修复并验证关闭 | delivery-model gap |
| KAFA-P0-4 | P0 | 低层 delivery gate 未复用高层流程前置条件 | 已修复并验证关闭 | contract divergence |
| KAFA-P1-1 | P1 | medium finding/failure-mode 策略不一致 | 已修复并验证关闭 | policy/runtime mismatch |
| KAFA-P1-2 | P1 | 状态域与 JSON schema 契约不闭合 | 已修复并验证关闭 | schema contract gap |
| KAFA-P1-3 | P1 | phase/scope 内部状态与公开 CLI journey 不闭合 | 已修复并验证关闭 | public workflow gap |
| KAFA-P1-4 | P1 | execution 环境 provenance 不完整 | 已修复并验证关闭 | assurance limitation |
| KAFA-P2-1 | P2 | committed live Codex evidence 不是当前 HEAD | clean committed candidate 证据已刷新并验证 | stale evidence |
| KAFA-P2-2 | P2 | release/供应链治理不完整 | 本地实施与治理响应已关闭；远端发布激活 not-run | release governance |
| KAFA-P2-3 | P2 | 缺少 outcome metrics | 本地指标契约与固定回归已关闭；field window not-run | adoption/effectiveness gap |

## 复现证据摘要

所有复现均在 `/tmp` 隔离项目中完成，没有迁移或修改真实业务项目。

| 场景 | 关键前置事实 | 实际结果 |
| --- | --- | --- |
| 空交付图 | requirement=0、acceptance=0、baseline=0、task=0、phase=`intake`、scope=`unconfirmed` | `delivery record` 成功；delivery=1；`validate --delivery` 返回 0 |
| cancelled 覆盖 | 1 个 acceptance 只链接 1 个 `cancelled` task；validation/gate 为 pass | trace、delivery record、delivery validation 均返回 0 |
| 无资格 target | acceptance 为“过期银行卡必须被拒绝”，target 只断言 `2 + 2 == 4` | validation 被挂到该 acceptance；delivery record 和 delivery validation 均返回 0 |
| medium 风险 | uncovered `medium` failure mode、open `medium` finding、gate residual risk 为空 | delivery record 和 delivery validation 均返回 0 |
| 非法 requirement 状态 | 公开 CLI 写入 `--status nonsense` | 写入成功；普通 doctor/validate 通过 |
| 非法 acceptance 状态 | DB 中写入 `nonsense` 后重建 projection | doctor 和普通 validate 均返回 0 |
| 非法 failure-mode 状态 | DB 中写入 `nonsense` 后重建 projection | doctor 返回 1，并同时报告 enum、schema 和 invariant 错误 |

隔离证据目录：

- `/tmp/kafa-p0-min-graph.LZnoYT`
- `/tmp/kafa-audit-p0-cancelled-20260720-a`
- `/tmp/kafa-audit-p0-qualification-20260720-a`
- `/tmp/kafa-audit-p1-medium-20260720-a`
- `/tmp/kafa-p1-status.5Hd8dt`
- `/tmp/kafa-audit-state-acceptance-20260720.a6b6EO`
- `/tmp/kafa-audit-state-fm-20260720.f3tDm4`

这些临时目录只用于本次本机审计，不能作为长期验收产物。正式修复必须把场景转成仓库内可重复的自动化测试。

## P0：必须先关闭

### KAFA-P0-1：强制最小可交付图

**关闭状态：已修复并验证，P0 stop-ship 已解除。**

Canonical spec 要求 delivery readiness 使用 active cycle/current candidate，并要求 accepted tasks、linked passing validations、immutable executions、blocking finding 处理和最新 gate（`openspec/specs/local-delivery-kernel/spec.md:83-87`）。当前 evaluator 只有在已存在非 cancelled requirement 时才执行 traceability 和 baseline 检查（`plugins/codex-project-harness/core/delivery.py:517-523`），因此零 requirement 会跳过两组门禁。

`record_delivery()` 只调用该 evaluator，并未独立要求 requirement、acceptance、baseline、confirmed scope 或 delivery readiness phase（`plugins/codex-project-harness/scripts/harness_db.py:2121-2151`）。这使得一个只有 test execution 和 pass gate 的 `intake` 项目可以被关闭为 delivered。

**完成定义与验收标准**

- [x] 先增加 `delivery without minimum graph` 红测，当前实现上必须稳定复现失败预期，而不是依赖 sleep、随机 UUID 或网络。
- [x] 交付前至少存在 1 个当前 cycle 的非 cancelled requirement；零 requirement 返回稳定 blocker，例如 `delivery-prerequisite:missing-requirement`。
- [x] 每个 active requirement 至少链接 1 个 active acceptance；每个 active acceptance 必须链接到 requirement，孤立 acceptance 不得被忽略。
- [x] 当前 cycle 存在 current frozen baseline，且 baseline 与当前 requirement/acceptance/failure-mode revision 一致。
- [x] scope 已被明确确认，且 `record_delivery` 时 project 处于合法 delivery-readiness 状态。
- [x] 每个 active acceptance 至少由 1 个 accepted task 和 1 个 qualified current-candidate passing validation 覆盖。
- [x] 任一前置条件缺失时，`delivery record` 返回非零，不新增 delivery row，不关闭 cycle，不改变 project phase/status。
- [x] `validate --delivery` 对同一缺口返回非零，并报告与 `delivery record` 相同的稳定 blocker code。
- [x] 完整合法的 greenfield quickstart 和 documented full journey 仍可成功交付；不能靠 fixture-only 或直接 SQL 证明正向路径。

**必需自动化场景**

- 无 requirement；
- 有 requirement、无 acceptance；
- 有 requirement/acceptance、无 link；
- 无 baseline或 baseline stale；
- scope unconfirmed；
- phase 为 intake/implementation；
- active acceptance 没有 accepted task；
- 合法完整图正向通过。

### KAFA-P0-2：cancelled task 不得充当完成覆盖

**关闭状态：已修复并验证，P0 stop-ship 已解除。**

`traceability_issues()` 把 `accepted` 和 `cancelled` 都视为 completed task（`plugins/codex-project-harness/core/cycle_ledger.py:228-238`）；delivery evaluator 也只阻断不在 `accepted/cancelled` 集合内的 task（`plugins/codex-project-harness/core/delivery.py:538-543`）。因此唯一覆盖 acceptance 的 task 即使被取消，仍可形成 delivered。

**完成定义与验收标准**

- [x] 先增加“唯一 task cancelled + pass validation + pass gate”红测；`trace validate`、`delivery record`、`validate --delivery` 都必须失败。
- [x] 只有 `accepted` task 可以满足 acceptance 的 completed-task coverage；`cancelled` 只保留审计事实。
- [x] cancelled task 本身不应无条件阻断整个 cycle；当同一 acceptance 另有 accepted task 完整覆盖时，可以继续评估其他门禁。
- [x] cancelled task 的旧 validation 可以保留审计价值，但不能单独使 acceptance qualified 或 delivered。
- [x] task 从 submitted 转为 cancelled 后，依赖它的 qualification/validation/gate 必须失效或在 delivery evaluator 中明确不再满足覆盖。
- [x] trace projection、CLI `trace validate`、quickstart status 和 delivery evaluator 对 cancelled 的语义一致。

**必需自动化场景**

- sole cancelled task 必须阻断；
- cancelled + accepted task 覆盖同一 acceptance 时，cancelled 不制造额外 blocker；
- unrelated cancelled task 不阻断另一个完整交付图；
- accepted task 取消后，旧 delivery evidence 不再满足当前交付。

### KAFA-P0-3：引入 qualified target / qualified validation

**关闭状态：资格模型已实施并验证，P0 stop-ship 已解除。**

Target 的 gateability 只验证 target kind 和 command policy（`plugins/codex-project-harness/scripts/harness_db.py:1508-1566`）。`verify_run()` 只要求 target 和 acceptance 分别存在，然后把二者写入 execution/validation（`plugins/codex-project-harness/scripts/harness_db.py:1638-1665, 1736-1803`）。Delivery 只查询 acceptance_id 下是否有 passing immutable execution（`plugins/codex-project-harness/core/delivery.py:570-596`），不会验证 target 是否有资格证明该 acceptance。

这是“显式、可审查的资格事实”缺失，不应被误解为要求 Kernel 自动理解任意自然语言测试是否相关。Kafa 无法可靠地从 `2 + 2 == 4` 和“拒绝过期银行卡”推断业务语义；它能且必须做到的是：没有明确资格声明和独立审查时不允许二者形成交付证据。

**完成定义与验收标准**

- [x] OpenSpec 明确定义 acceptance-target qualification 的 canonical fact；不得只靠 validation 上的自由 `acceptance_id` 表示资格。
- [x] Qualification 至少绑定 cycle、acceptance ID/revision、target ID/definition digest、qualification rationale、记录者以及创建时间。
- [x] `verify run --acceptance AC1 --target X` 在不存在 current qualification 时返回非零，且不写 execution/validation 交付事实。
- [x] Target command、kind、result format、sandbox policy 或 acceptance criterion/revision 变化时，旧 qualification 和依赖 validation 自动失效。
- [x] Delivery evaluator 必须通过 qualification -> validation -> immutable execution 的完整 join 取证，不能只按 acceptance_id 查询任意 pass。
- [x] Independent gate 必须审查 acceptance-target mapping；same-context 自动声明不能把语义资格提升为独立确认。
- [x] Quickstart 如自动建立 qualification，必须把它明确标为用户输入形成的 procedural mapping，并在独立 gate 前保持未独立确认状态。
- [x] Projection 和审计输出能够显示“哪个 target、哪个定义 digest、基于什么理由证明哪个 acceptance”。
- [x] 文档明确剩余边界：显式 qualification 提供可审计责任链，不是自然语言语义证明或不可伪造 provenance。

**必需自动化场景**

- target 和 acceptance 都存在但无 qualification 时 verify 失败；
- qualified target 的 current execution 可以覆盖 acceptance；
- target 修改后旧 qualification/validation 失效；
- acceptance 修改后旧 qualification/validation 失效；
- 一个 target 不得因为对 AC1 qualified 而自动覆盖 AC2；
- unrelated fixture 在没有显式 qualification 时不能 delivered。

### KAFA-P0-4：所有 delivery surface 复用同一前置条件

**关闭状态：共享 evaluator 与公开 journey 已实施并验证，P0 stop-ship 已解除。**

高层 `transition_phase()` 和 `quickstart_minimal()` 会检查或建立 requirement、acceptance、baseline、scope 和 phase（`plugins/codex-project-harness/scripts/harness_db.py:907-980, 3303-3360`）。低层 `record_delivery()` 不复用这些状态；`validate_delivery(..., require_phase=False)` 的 `require_phase` 参数未被使用（`plugins/codex-project-harness/scripts/harness_db.py:3013-3020`）。结果是高层 journey 较严格，而低层 API/CLI 可以绕过。

**完成定义与验收标准**

- [x] 建立一个 canonical、纯读取、返回结构化 blocker codes 的 delivery prerequisite evaluator。
- [x] `transition_phase(... delivery_readiness)`、`quickstart status`、`validate --delivery` 和 `record_delivery()` 必须复用该 evaluator，禁止各自复制 SQL 条件。
- [x] 删除未生效的 `require_phase` 参数，或让它具有被测试的明确语义；不得保留“看似启用、实际忽略”的开关。
- [x] 进入 `delivery_readiness` 的评估模式不得要求“已经处于 delivery_readiness”，避免循环依赖；`record_delivery` 模式必须额外要求当前 phase 已合法进入 delivery_readiness。
- [x] delivery 记录后的 consistency 模式允许 cycle 为 delivered，但必须验证 delivery row、candidate、phase 和关闭事实一致。
- [x] 对同一个 fixture，四个 surface 的 blocker code 集合一致；仅允许因动作阶段不同而增加已文档化的 phase/cycle blocker。
- [x] 所有 low-level API 直接调用测试必须 fail closed，不能只测 CLI 包装器。
- [x] P0 改动后至少有一条公开、文档化的非内部调用路径能够合法达到所有前置条件；如果不在本批公开 phase/scope 命令，则必须用派生状态或受支持的确认入口闭环。

**必需自动化场景**

- 相同缺口分别调用 phase transition、quickstart status、validate 和 record，断言相同 blocker；
- API 直接调用不能绕过 CLI；
- 进入 delivery readiness 与记录 delivery 的两阶段无循环依赖；
- delivery 写入过程中 candidate 改变仍保持原有 fail-closed；
- 完整 public journey 正向通过。

## P0 实施顺序与退出门

修复顺序保持为：

1. `KAFA-P0-1` 强制最小可交付图；
2. `KAFA-P0-2` 禁止 cancelled 充当完成覆盖；
3. `KAFA-P0-3` 引入 qualified target/validation；
4. `KAFA-P0-4` 把 1-3 的规则收口为唯一 evaluator 并接入全部 surface。

开始 production 修改前必须先建立上述红测。P0 只有同时满足以下条件才可关闭：

- [x] 每个 P0 场景均有“修复前红、修复后绿”的确定性证据；
- [x] delivery targeted suite、traceability、task lifecycle、schema30 runtime、quickstart journey 全绿；
- [x] 完整 `unittest discover` 全绿，ResourceWarning 视为错误；
- [x] runtime smoke、fixture/stability E2E、结构检查、JSON 检查、OpenSpec validation 和 `git diff --check` 全绿；
- [x] 两个独立只读 QA 分别审查 delivery graph/qualification 和 lifecycle/state-machine；
- [x] skipped、blocked、not-run、fixture-only 不计为通过；
- [x] 新审计明确列出剩余风险，尤其是 qualification 的 procedural 语义边界。

P0 red/green history is recorded in
`docs/audits/2026-07-20-delivery-integrity-hardening-baseline.md`. The final
whole-change discovery supersedes the earlier checkpoint: 786 total, 772
passed, 14 explicitly separated skips, zero failures/errors/expected failures.
The 14 skips are not counted as passes; 12 are Windows-only path contracts and
two require artifact paths, with the latter rerun separately against the real
wheel/sdist as 3/3 passing tests. Independent reviews found and verified the
additional lifecycle, historical-audit, migration and finding-scope closures
listed in the final dated audit.

## P1：第二批修复

### KAFA-P1-1：统一 medium finding / failure-mode 语义

**关闭状态：已修复并验证。**

`independent-quality-gate` Skill 明确写明 medium finding 需要显式 residual-risk acceptance，same-context review 需要清晰 residual-risk notes（`plugins/codex-project-harness/skills/independent-quality-gate/SKILL.md:66-72`）。Runtime 只把 high/critical failure modes 和 findings 纳入 blocking/accepted-risk 检查（`plugins/codex-project-harness/core/delivery.py:598-679`）。

**验收标准**

- [x] open medium finding 未解决、未 false-positive、未被结构化接受时阻断 delivery；
- [x] active medium failure mode 没有 qualified current-candidate coverage 时阻断 delivery；
- [x] medium acceptance/exemption 记录 actor、reason、scope、current revision/candidate 和 expiry，并在过期或 revision 变化后失效；
- [x] same-context-degraded 对 low/medium 要求非空、可定位的 residual-risk notes；
- [x] low 风险现有语义保持不变，除非 OpenSpec 明确修改；
- [x] Skill、canonical spec、CLI help、runtime 和测试使用相同措辞和状态。

### KAFA-P1-2：闭合状态域与 schema 契约

**关闭状态：已按校准范围修复并验证。**

公开 CLI 的 requirement `--status` 没有 choices（`plugins/codex-project-harness/scripts/harness.py:158-165`），schema guard 只检查非空（`plugins/codex-project-harness/core/schema_guard.py:38-42`），DDL 无 CHECK，runtime doctor 的 enum 清单也不包含 requirement/acceptance status（`plugins/codex-project-harness/scripts/harness_db.py:2810-2827`）。实际已复现 requirement 和 acceptance 的 `nonsense` 状态通过 doctor/validate。

Failure-mode 与此不同：public API/CLI、JSON schema、doctor 已有枚举保护，非法 DB 状态会被 doctor 拒绝。它仍缺 DB CHECK，但不是同等级公开写入绕过。

当前 16 个 JSON schema 都声明 Draft 2020-12，但没有 `$id`；`additionalProperties` 在 true、false、缺省之间不一致。手写 validator 只覆盖 required、基础 type、enum、array item type 和 `additionalProperties=false`（`plugins/codex-project-harness/scripts/harness_db.py:2874-2900`），不能被描述为完整 Draft 2020-12 实现。

**验收标准**

- [x] 在 canonical spec 中定义 requirement、acceptance、failure-mode 的唯一合法状态集合和迁移兼容规则；
- [x] `requirement --status nonsense` 在 argparse/API guard 前置失败，返回非零且不写 DB；
- [x] 非法 requirement/acceptance/failure-mode 状态在 DB/doctor/schema contract 层均可检测；
- [x] 每个公开 JSON schema 有稳定、唯一、版本化 `$id`；
- [x] 每个 schema 明确 closure policy；允许扩展的 schema 必须记录兼容理由，不能靠缺省行为；
- [x] schema 方言收缩到 Kernel 完整实现并结构校验的 closed subset，不再声称未实现的完整 Draft 语义；
- [x] DB-level 约束通过 schema 31 OpenSpec 与可回滚 migration 引入，没有静默重定义 schema 30；
- [x] schema fixture、migration fixture、doctor 和 JSON validation 覆盖所有状态枚举及未知字段策略。

### KAFA-P1-3：闭合公开 phase/scope journey

**关闭状态：已修复并验证公开 journey。**

内部存在 `transition_phase()` 和 `confirm_scope()`（`plugins/codex-project-harness/scripts/harness_db.py:907-1000`），`quickstart minimal` 会调用它们。公开 CLI 没有 phase/scope 子命令，且 Skill 明确把 stages 定义为 workflow stages、不是 public CLI state。与此同时 `QUICKSTART.md` 的完整手工 journey 直接记录 requirement/baseline/task/gate/delivery，没有公开 scope confirmation 或 phase transition 步骤。

这不必通过重新暴露旧 phase CLI 修复；但 runtime、public journey 和持久状态必须选择一个一致模型。

**验收标准**

- [x] OpenSpec 明确采用受支持的 baseline confirmation 与 readiness 入口，不暴露任意 phase mutator；
- [x] 非 quickstart 用户仅使用公开 CLI 就能合法达到 P0 delivery prerequisites；
- [x] 公开 full journey 作为真实 subprocess E2E 执行，而不是只做文档字符串检查；
- [x] public CLI、Skill、README 和内部 DB 对当前 readiness/scope 语义一致；
- [x] 不恢复被 local-core-slimming 明确删除的第二套 Host task/model/worktree lifecycle。

### KAFA-P1-4：补强 execution 环境 provenance

**关闭状态：已补强并验证 schema-31 execution provenance。**

Execution 已持久化 command、candidate、stdout digest、artifact、count、result format、semantic status、runner、sandbox/no-network 和 policy status。但 execution schema 没有 OS/runtime/container engine/image digest 等字段（`plugins/codex-project-harness/schemas/execution.schema.json:7-42`）。ContainerExecutor 运行 Docker/Podman 和用户给出的 image 字符串，却不把 engine facts 或 resolved image digest 写入 CommandResult/DB（`plugins/codex-project-harness/core/execution.py:1113-1336`）。Gateable target 默认仍为 `result_format=regex`。

**验收标准**

- [x] Container execution 持久化 engine 类型和版本、requested image reference、resolved immutable image digest；无法解析时 fail closed；
- [x] Local execution 持久化可复核的 OS/platform、runtime executable/version/digest 和 policy version；
- [x] Execution 绑定 target definition digest，target 变化不只依赖行值比较；
- [x] medium/high/critical unit/integration failure-mode coverage 采用 supported structured result；regex 仅保留在文档化 low-risk 路径；
- [x] regex 不得覆盖 medium/high/critical failure mode；
- [x] provenance 新字段保持 execution immutable，并有 schema/migration/回滚和跨平台契约测试。

P1 checkpoint evidence remains in the baseline audit. On the final candidate,
the 786-case discovery completed with zero failure/error; the available local
container capability executed rather than being promoted from an older skip.
The late release/supply-chain/rehearsal targeted suite passed 42/42. Final
independent bounded review and all six resolved PR threads left zero open
Critical/High/Medium finding.

## P2：治理与采用层

### KAFA-P2-1：刷新并正确标记 live Codex evidence

**关闭状态：clean committed candidate 证据已刷新。**

single 与 parallel 报告现在都绑定 clean committed executable source
`git_head=d795622...`、空 status digest
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`、
`workspace_sha256=222ee103c2d33599575217feead6b4f7a4bd733f6f6bd9cf49ecf19c1904ff54`
和同一 Codex binary digest。两者分别通过 1/1，0 skip/fail，persistent
consistency errors 均为空，并进入最终 786-case discovery。

**验收标准**

- [x] P0/P1 稳定后，在 exact current candidate 重跑 single 和 parallel real Native Codex profiles；
- [x] clean committed reviewed candidate evidence：报告 source/status 均为 clean；
- [x] 报告的 source/status/binary/token/scope/timing 重新计算并通过一致性验证；
- [x] persistent report 与审计明确区分 historical、current candidate、fixture 和 not-run；
- [x] dirty source 报告不被标记为 clean committed release evidence；
- [x] live runner 的失败或未运行状态必须如实保留，不以 fixture 替代。

### KAFA-P2-2：补齐 release 与供应链治理

**关闭状态：本地供应链实施和 main 治理响应已验证；远端发布激活 not-run。**

仓库 metadata 和 README 声明 MIT，但根目录没有 LICENSE 文件；GitHub API 返回 repository license 为 null。Release workflow 会生成 SHA256SUMS（`.github/workflows/release.yml:176-187`），但仓库没有 SBOM、签名或 build provenance/attestation。2026-07-20 的 GitHub 只读查询返回 `main` branch not protected，repository rulesets 为 `[]`。

**验收标准**

- [x] 添加完整 MIT LICENSE，并使本地 package metadata、README、wheel/sdist metadata 一致；
- [ ] GitHub 远端 license detection：默认分支当前 API 仍为 `null`，需合并后由 GitHub 刷新，pre-merge 记为 `not-run`；
- [x] wheel 和 source artifact 生成标准 CycloneDX SBOM，且 SBOM 与 artifact digest 一起保留；
- [x] 本地 provenance 记录 builder、source commit/status 和 artifact digest；release workflow 已锁定官方 build-attestation action；
- [ ] 远端 GitHub attestation 产物：未获 tag/release 授权，记为 `not-run`；
- [x] main 启用 branch protection，要求 PR、1 个 review、conversation resolution 和 Ubuntu/macOS/Windows required checks；
- [x] release rehearsal 在不创建 tag/release、不 publish 的条件下验证 build、SBOM、provenance、checksum 和 isolated install；
- [x] 真正 tag/release/deploy 仍需单独用户授权，本轮均未触发。

最终 clean source candidate 持久制品位于
`/private/tmp/kafa-final-audit-3c881f5/`。Wheel 为 48,085 B、SHA-256
`f5b2f10ae2746d0a513f1761f0eadae59be321ae2ea32cd0a9a11c1a1d0668dd`；
sdist 为 503,198 B、SHA-256
`c97814939098937ebb031f5a97e17a170790fd1d9a3bceb62f21ca3bacd15690`。
两份 CycloneDX、SHA256SUMS、local in-toto provenance 与 manifest 已独立
verify；真实 artifact license tests 3/3、isolated install smoke 均通过。
Assurance 明确为 `unsigned-local-integrity-statement`。

### KAFA-P2-3：建立 outcome metrics

**关闭状态：本地指标契约与固定回归已实现；field observation window not-run。**

当前审计和 benchmark 主要测表数、LOC、体积、延迟、测试数、完整性和证据一致性。`project-retrospective` 只询问“what caused rework / what should be measured next time”，没有版本化 outcome 指标、基线、分母和观察窗口。

**验收标准**

- [x] 定义 false-green prevented、post-delivery escaped defect、rework rate、rollback/recovery success、time-to-verified-delivery 和 qualification coverage；
- [x] 每个指标有事件定义、分子、分母、观察窗口、缺失数据语义和不适用条件；
- [x] 建立固定四场景修复前 baseline，并在当前实现上复测为 4/4 fail-closed；
- [x] integrity test count、fixture pass 和 outcome improvement 不互相替代；field 数据仍为 `not-run`/null，且 `field_improvement_claimed=false`；
- [x] 保持 local-only：只使用本地、可审计、可选择导出的聚合，不新增远程 telemetry 或 Connector。

最终固定回归报告绑定 clean executable workspace
`222ee103c2d33599575217feead6b4f7a4bd733f6f6bd9cf49ecf19c1904ff54`：
historical before 4/4 false-delivery，current after 4/4 fail-closed，closure
rate 1.0。六项 field metric 仍全部是 `not-run/null`，且
`field_improvement_claimed=false`。

## 后续 OpenSpec 与授权门

P0 属于跨 delivery、traceability、schema relation、CLI 和文档的长期行为变更，production 修改前必须创建新的 OpenSpec change。Change 的 proposal/design/spec/tasks 至少应：

- 以本文件 4 个 P0 ID 为 stop-ship scope；
- 将 P0 验收条目转成唯一 task checklist；
- 明确 qualification 是 procedural accountability，不承诺自动语义证明；
- 处理 delivery-readiness transition 与 record-delivery phase check 的非循环模型；
- 说明 schema 30 是否保持不变；如需 schema 变更，先设计 backup、dry-run、rollback 和旧项目兼容；
- 保持 Native Codex/ChatGPT 对 task、subagent、worktree、approval、model、cancel 和 handoff 的唯一 ownership；
- 不引入外部 Connector、网络依赖或伪造 provenance。

本清单本身不授权 commit、push、merge、tag、release、deploy、用户级安装替换或业务项目迁移；
后续 commit/push/普通 merge 授权单独记录在最终审计，tag/release/deploy 仍未获授权。

## 本次审计验证

- Pre-archive `openspec status --change delivery-integrity-hardening`：4/4
  artifacts；change validation passed。归档后 active changes 为 0，最新
  `openspec validate --all --strict --no-interactive`：1/1 canonical spec passed。
- `gh api --help` 在治理写入前用于核对方法；历史基线是 protection HTTP
  404 / rulesets `[]`，获授权后的 readback 证明 main 已要求 PR、1 review、
  conversation resolution、strict Ubuntu/macOS/Windows checks 与 admin
  enforcement。该治理写入单独披露，不是 business runtime 网络依赖。
- Release/supply-chain/rehearsal targeted：42/42 passed；最终 786-case
  discovery：772 passed、14 skipped、0 failure/error。
- Runtime smoke 2/2、Skill markers 22/22、fixture 6/6、stability 11/11、
  Native single/parallel 各 1/1、固定 outcome 4/4。
- Plugin structure、全部 JSON、schema-31 contract、local-only/Host ownership
  contracts 与 `git diff --check` 均通过。
- 所有 reproduction、migration、install 和 rollback 均在临时目录完成；未迁移
  用户或业务项目数据库。

# Quick Start

Codex Project Harness（Kafa）提供一条 local-only verified delivery 路径：OpenSpec
在需要时锁定规格，Native Codex/ChatGPT 完成可见的本地代码工作，Kafa 根控制器
在项目本地记录事实并验证当前 candidate。

## 1. 安装 plugin

在 Kafa 源码仓库根目录运行：

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

重启 Codex，从 `kafa-local` marketplace 安装 `codex-project-harness`。User-scoped
安装、升级和 schema 迁移细节见 [INSTALL.md](INSTALL.md)。

## 2. 初始化本地项目

普通业务项目只使用稳定的 `kafa project ...` 入口：

```bash
kafa project init --repo .
kafa project status --repo .
kafa project doctor --repo .
```

初始化会创建 schema 31 SQLite 事实源、local Markdown views，以及三个静态 Native
Codex agent templates。它不需要远程凭证，也不会替你启动 task、subagent 或 worktree。

## 3. 需要规格时先走 OpenSpec

需求不清晰、中大型功能、架构或跨模块调整、长期产品行为先建立或读取 OpenSpec
change：

```bash
openspec status --change <change-name>
openspec validate <change-name>
```

依次确认 proposal、design、spec 和 `tasks.md`。实施时以 `tasks.md` 为唯一清单。
验收明确的小型改动可直接选择对应 Kafa Skill，不强制创建 change。

<!-- BEGIN GENERATED: workflow-contract:happy-path -->
## Verified Patch Happy Path

The delivery plan creates only the linked planning graph. Baseline confirmation, controller verification, task acceptance, quality review, readiness, and delivery remain explicit.

1. **Delivery plan** (`delivery-plan`): atomically create the linked local plan graph
2. **Baseline confirmation** (`baseline-confirmation`): explicitly freeze and confirm current scope
3. **Acceptance-target qualification** (`qualification`): bind acceptance revision and target digest with rationale and actor
4. **Task start** (`task-start`): root controller explicitly starts the generated planned task
5. **Task submission** (`task-submit`): root controller inspects returned code and records producer context
6. **Controller verification** (`controller-verification`): run the qualified target and persist immutable current-candidate evidence
7. **Task acceptance** (`task-accept`): accept only after submitted code and controller verification are complete
8. **Quality gate** (`quality-gate`): record reviewer findings, qualifications, and residual risk
9. **Delivery readiness** (`delivery-readiness`): reuse the canonical prerequisite evaluator before phase transition
10. **Delivery record** (`delivery-record`): record the fact-derived verified local handoff; compatibility prose flags are supplemental and deployment remains excluded
11. **Delivery validation** (`delivery-validation`): re-evaluate delivered consistency on the recorded candidate

Dependency edges:

- `delivery-plan` → `baseline-confirmation`
- `delivery-plan` → `qualification`
- `delivery-plan` → `task-start`
- `task-start` → `task-submit`
- `qualification` → `controller-verification`
- `task-submit` → `task-accept`
- `controller-verification` → `task-accept`
- `task-accept` → `quality-gate`
- `baseline-confirmation` → `delivery-readiness`
- `quality-gate` → `delivery-readiness`
- `delivery-readiness` → `delivery-record`
- `delivery-record` → `delivery-validation`

```bash
kafa project init --repo .
kafa project quickstart --repo . status
kafa project quickstart --repo . delivery-plan --file delivery-plan.json --json
kafa project baseline --repo . confirm --id BL1 --summary 'confirmed scope' --by root-controller
kafa project task --repo . start PATCH-T1
kafa project quickstart --repo . verified-patch --id PATCH --json
kafa project task --repo . submit PATCH-T1 --context-id producer-context --evidence 'root inspected returned code'
kafa project task --repo . accept PATCH-T1 --evidence 'verification and review complete'
kafa project gate --repo . record --reviewer-context fresh --reviewer-context-id reviewer-context --result pass --qualification PATCH-Q1
kafa project delivery --repo . ready
kafa project delivery --repo . record --scope 'verified local handoff' --handoff 'return code and residual risks'
kafa project validate --repo . --delivery
```

`verified-patch` reuses the immutable `verify run` transaction only. It always reports task, gate, and delivery status and never creates a Host lifecycle or passing gate.
<!-- END GENERATED: workflow-contract:happy-path -->

## 4. 风险与证据边界

- 只有 controller executor 针对当前 candidate 产生的有效 structured execution
  才是命令证据；退出码为零但结果缺失、语义失败或通过数为零仍然 fail closed。
- Qualification 是 acceptance revision 与 target digest 的程序性映射，不是自然语言
  语义证明或独立 provenance。
- Medium 风险需要 qualified structured current-candidate coverage 或完整、当前、未过期
  的 accepted/exempt metadata。
- High/critical 同时要求 structured current-candidate execution、精确
  `reviewed-local` 和不同的 producer/reviewer context；没有独立可验证 provenance 时
  仍为 `human-review-required`。
- `skipped`、`blocked`、`not-run`、fixture-only 和零测试数都不等于通过。

## 5. 管理和恢复

先读实际 help，再执行管理写入：

```bash
kafa project migrate --repo . --help
kafa project repair --repo . --dry-run
kafa project projection --repo . rebuild
```

`repair` 在 mutation 前创建 verified SQLite backup。`projection rebuild` 只从 SQLite
重建 local Markdown views；compact audit events 和 Markdown 都不是数据库恢复来源。
遇到 `recovery-required` 或 `rollback-incomplete` 时保留 sentinel、manifest、DB 和
projection backup，按 [INSTALL.md](INSTALL.md) 恢复，绝不通过删除 sentinel 伪装成功。

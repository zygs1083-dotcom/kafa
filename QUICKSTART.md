# Quick Start

Codex Project Harness（Kafa）提供一条 local-only verified delivery 路径：OpenSpec 在需要时锁定规格，Native Codex/ChatGPT 完成可见的本地代码工作，Kafa 根控制器在项目本地记录事实并验证当前 candidate。

## 1. 安装 plugin

在 Kafa 源码仓库根目录运行：

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

重启 Codex，从 `kafa-local` marketplace 安装 `codex-project-harness`。

如果需要 user-scoped 安装：

```bash
python3 -m pip install -e .
kafa plugin install --scope user --repo .
codex plugin add codex-project-harness@kafa-local
kafa doctor --scope user --repo .
```

安装和 schema 迁移细节见 [INSTALL.md](INSTALL.md)。

## 2. 初始化本地项目

在业务项目中运行 consolidated `project-harness` proxy：

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . init

python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . status

kafa project doctor --repo .
```

初始化会创建 schema 31 SQLite 事实源、local Markdown views，以及三个静态 Native Codex agent templates。它不需要远程凭证，也不会替你启动 task、subagent 或 worktree。

## 3. 最小闭环

如果目标、验收和测试命令已经明确，用一个命令建立并执行最小交付闭环：

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

然后检查状态和交付不变量：

```bash
python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . quickstart status

python3 /path/to/kafa/plugins/codex-project-harness/skills/project-harness/scripts/harness.py \
  --root . validate --delivery
```

只有 controller executor 针对当前 candidate 产生的有效 structured execution 才是命令证据；该 immutable execution 记录后不可覆盖。退出码为零但结果缺失、格式错误、语义失败或通过数为零，都必须 fail closed。

## 4. 需要规格时先走 OpenSpec

以下情况先建立或读取 OpenSpec change：需求不清晰、中大型功能、架构调整、跨模块变更，或需要长期维护的产品行为。

```bash
openspec status --change <change-name>
openspec validate <change-name>
```

依次确认 change 的 proposal、design、spec 和 `tasks.md`。实施时以 `tasks.md` 为唯一清单，按依赖顺序推进并及时更新 checkbox。Kafa 保存交付所需的最小事实，不复制一套 OpenSpec 文档作为运行时权威。

验收明确的小型安全改动不强制创建 OpenSpec change，可以直接使用 `minimal-safe-change`、`bug-fix-loop` 或 `test-first-delivery`。

## 5. 完整本地交付 journey

下面使用源码仓库中的统一 CLI；如果 plugin 安装在项目外，也可以把入口替换为前文的 `project-harness` proxy。

先记录需求、验收和失败模式：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . requirement add \
  --id R1 \
  --kind functional \
  --body "The requested behavior is implemented locally"

python3 plugins/codex-project-harness/scripts/harness.py --root . acceptance add \
  --id AC1 \
  --criterion "The registered test target passes against the current candidate"

python3 plugins/codex-project-harness/scripts/harness.py --root . requirement link \
  --requirement R1 \
  --acceptance AC1

python3 plugins/codex-project-harness/scripts/harness.py --root . failure-mode add \
  --id FM1 \
  --feature "Requested behavior" \
  --scenario "Invalid input reaches the implementation" \
  --trigger "Input violates the documented contract" \
  --expected "The implementation fails safely" \
  --risk low \
  --acceptance AC1

python3 plugins/codex-project-harness/scripts/harness.py --root . baseline confirm \
  --id B1 \
  --summary "R1 and AC1 are the explicitly confirmed scope" \
  --by root-controller
```

建立 task 与 test target：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . task add \
  --id T1 \
  --task "Implement R1" \
  --acceptance AC1 \
  --failure-mode FM1

python3 plugins/codex-project-harness/scripts/harness.py --root . test-target add \
  --id UNIT \
  --kind unit \
  --command-template "python3 -B -m unittest discover -s tests -p 'test_*.py'"

python3 plugins/codex-project-harness/scripts/harness.py --root . test-target link \
  --task T1 \
  --target UNIT

python3 plugins/codex-project-harness/scripts/harness.py --root . test-target qualify \
  --id Q1 \
  --target UNIT \
  --acceptance AC1 \
  --rationale "UNIT directly exercises the AC1 behavior" \
  --by root-controller

python3 plugins/codex-project-harness/scripts/harness.py --root . task start T1
```

完成并关闭一个 cycle 后，新的 cycle 会把 scope 重置为 unconfirmed。
`requirement`、`acceptance`、`task` 的标签可以在新 cycle 中复用；baseline、
finding、qualification 等全局事实 ID 必须使用新值。`quickstart minimal`
会在非默认 cycle 自动为 target、qualification 和 baseline 加入 cycle 标识。
若要只读复核旧交付而不切换当前 cycle：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle audit \
  --id CYCLE-current --json
```

此时由 Native Codex/ChatGPT 决定是否创建 task、subagent 或 worktree，以及使用什么 model、approval 和 cancel/handoff 行为。Kafa 不创建第二套生命周期。执行者只修改获准的项目文件并把 diff、测试建议、上下文标识和风险返回给根控制器；执行者不修改 `.ai-team/state/harness.db`。

根控制器回到目标 workspace 后检查真实 candidate，再推进 task 并独立验证：

Qualification 只是将当前 acceptance revision 与 target definition digest 绑定的可审计流程责任记录；它不会自动证明测试与业务语义相符，也不是密码学 provenance。当 acceptance 或 target 的执行相关定义改变时，必须创建新 qualification 并重新执行。

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . task submit T1 \
  --context-id producer-context \
  --evidence "Root controller inspected the returned local diff"

python3 plugins/codex-project-harness/scripts/harness.py --root . verify run \
  --target UNIT \
  --acceptance AC1 \
  --failure-mode FM1
```

如果 target 要求本地容器 policy：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . verify run \
  --target UNIT \
  --acceptance AC1 \
  --failure-mode FM1 \
  --runner container \
  --container-image python:3.12-slim
```

这个示例的 `FM1` 是 low risk，因此简单 regex positive-count target 可以作为限定后的
覆盖。Medium/high/critical 的 unit 或 integration failure-mode 覆盖必须使用受支持的
structured result format 和正数、可对账的执行结果；不能用 regex 冒充结构化覆盖。

Schema 31 execution 会记录 `target_definition_sha256`、controller `platform`、
`runtime_executable`/`runtime_version`/`runtime_executable_sha256`、
`policy_version` 和 `provenance_status=complete`。Container runner 还要求镜像已在
受支持的本机 Docker 或 Linux native-local Podman，记录 engine/version、冻结的本地
`container_engine_endpoint`、requested image 与 `container_image_digest`，并把全部
daemon 调用固定到该 endpoint，再用 immutable identity 和 `--pull=never` 运行；Kafa
不隐式 pull。运行时还会覆盖镜像 entrypoint 为受控 `/bin/sh`，只读取该入口生成的
artifact，不把 engine CLI stdout 当作测试输出。Remote/ambiguous routing、engine 与
endpoint 类型不匹配、缺字段、runtime/endpoint/image 漂移或
`legacy-incomplete` history 都不能成为当前 delivery evidence。Go streaming result
缺少可对账的 terminal package event 或 started test 终态时 fail closed；
`cargo-nextest-json` 绑定 experimental libtest JSON v0.1，每个顺序 suite 必须有唯一、
可对账的 terminal event，允许 stress 产生多个完整 suite。事件乱序、缺失结果 artifact、
或 structured stdout 超过捕获上限时同样 fail closed。

先让根控制器接受已验证的 task，再记录审查 finding 和绑定当前 revision 的 quality
gate。不同 producer/reviewer context 是流程分离元数据，不是密码学身份：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . task accept T1 \
  --evidence "Current-candidate execution and review completed"

python3 plugins/codex-project-harness/scripts/harness.py --root . gate record \
  --reviewer-context fresh \
  --reviewer-context-id reviewer-context \
  --result pass \
  --qualification Q1 \
  --residual-risk "No unresolved low risk"
```

最后先通过同一 prerequisite evaluator 进入 readiness，再记录交付并重新验证：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . delivery ready

python3 plugins/codex-project-harness/scripts/harness.py --root . delivery record \
  --scope "R1 local verified code handoff" \
  --acceptance AC1 \
  --validation "Current-candidate controller execution passed" \
  --quality-gate "Independent local review recorded" \
  --handoff "Code and remaining risks returned to the user"

python3 plugins/codex-project-harness/scripts/harness.py --root . validate --delivery
python3 plugins/codex-project-harness/scripts/harness.py --root . status
```

如果 candidate 在 execution 或 quality gate 之后发生变化，旧记录保留审计价值，但不再满足当前交付条件。

Schema 31 不接受自由文本状态：requirement 与 acceptance 的合法值都是 `active/cancelled`，failure mode 的合法值是 `identified/accepted/exempt`。未知值会在 CLI/API 写入前或 migration preflight 中失败；只有 schema 30 的历史 failure-mode `active` 会被迁移为 `identified`。

## 6. High/critical 风险

High/critical failure mode 的本地路径必须同时具备：

- 当前 candidate 的 structured controller execution；
- target 要求的 sandbox/no-network metadata；
- 与 producer context 不同的 reviewer context；
- 无 open blocking finding。

即使具备这些本地事实，没有独立可验证 provenance 时，结果仍是 `human-review-required`，不能自动记录 delivered。用户明确接受或豁免风险时，必须完整记录 actor、reason、范围、revision 和 expiry；这是一条可审计的 procedural decision，不是独立身份或执行证明。

`skipped`、`blocked`、`not-run`、fixture-only 和零测试数都不等于通过。

## 7. 选择 Skill

| 场景 | Skill |
| --- | --- |
| 完整功能、OpenSpec change 或需要 verified handoff | `project-harness` |
| 小型、边界清晰、安全改动 | `minimal-safe-change` |
| 需要复现和回归验证的 bug | `bug-fix-loop` |
| 契约敏感或明确要求测试优先 | `test-first-delivery` |
| 独立 QA、finding 和 current-candidate 审查 | `independent-quality-gate` |
| 运行时或交付证据审计 | `harness-audit` |
| 完成一个交付里程碑后的复盘 | `project-retrospective` |

通常从 `project-harness` 开始；它包含 workspace 检查、OpenSpec 路由、需求基线、单写 task lifecycle、不可变验证和 delivery readiness。小型任务使用更窄的 Skill，避免引入不必要流程。

## 8. 管理和恢复

公开顶层 CLI 领域只有：

```text
init  status  doctor  quickstart
cycle  requirement  acceptance  failure-mode  baseline  trace
task  test-target  verify  validation
finding  gate  delivery  decision
validate  repair  migrate  projection
```

先读实际 help，再执行管理写入：

```bash
python3 plugins/codex-project-harness/scripts/harness.py --help
python3 plugins/codex-project-harness/scripts/harness.py migrate --help
python3 plugins/codex-project-harness/scripts/harness.py repair --dry-run
```

`repair` 在 mutation 前创建 verified SQLite backup。`projection rebuild` 只从 SQLite 重建 local Markdown views；compact audit events 和 Markdown 都不是数据库恢复来源。

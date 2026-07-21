# Kafa Delivery Integrity Hardening 最终审计

日期：2026-07-21

## 结论与权限边界

OpenSpec change `delivery-integrity-hardening` 的本地实现、迁移、回滚、
delivery integrity、Native E2E、制品、隔离安装、供应链、性能与独立 QA 已完成。
唯一实施清单已随 change 归档至
`openspec/changes/archive/2026-07-21-delivery-integrity-hardening/tasks.md`，原始缺陷与验收权威是
`docs/audits/2026-07-20-delivery-integrity-issue-checklist.md`。

最终 executable candidate 身份：

- branch：`main`；
- `HEAD=origin/main=e3d46d9feb850e2f2462cf6e6fd0ecb7016e66bb`；
- worktree：因未获 commit 授权而 intentionally dirty；
- evaluation status SHA-256：
  `41fcfbd12e5c6b02fc6a541599d7f4bfcb981f6e112ca7616ebad49a3bcb5e98`；
- evaluation workspace SHA-256：
  `f2f952f78008f24e4f9cf5c05d9a2c38ba258d41b8158ec2730f9d3965ddcb2e`；
- evaluation source entries：87；
- Native Codex binary SHA-256：
  `d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406`。

本 change 没有执行 commit、push、merge、tag、release、deploy、生产迁移、
业务项目迁移、secret 变更、付费资源或用户安装替换。用户单独授权过 Native
合成任务请求和 main branch protection；两者均在本文披露。

## 缺陷关闭映射

| ID | 修复前确定性问题 | 当前 fail-closed 合同 | 状态 |
| --- | --- | --- | --- |
| KAFA-P0-1 | 空 requirement/acceptance/baseline/scope/readiness 图仍可 delivery | requirement → acceptance → baseline/scope → accepted task → qualified validation/execution → gate → readiness 全图必需 | 本地关闭 |
| KAFA-P0-2 | sole cancelled task 可充当完成覆盖 | 只有有 evidence 与 accept actor/event 的 accepted task 可覆盖；cancelled 仅保留审计事实 | 本地关闭 |
| KAFA-P0-3 | 无关 gateable target 可冒充 acceptance 证据 | immutable qualification 绑定 acceptance revision、target digest、validation、execution 与 gate review | 本地关闭；qualification 仍是 procedural accountability |
| KAFA-P0-4 | 低层 `record_delivery()` 绕过高层前置条件 | direct API、CLI ready/record/validate 与 delivered audit 复用一个 structured evaluator | 本地关闭 |
| KAFA-P1-1 | open/uncovered medium 风险仍可 delivered | current structured coverage 或完整、未过期 accepted/exempt metadata；degraded review 必须写 residual risk | 本地关闭 |
| KAFA-P1-2 | nonsense 状态与薄 schema 可通过 | schema 31 在 CLI/API、DDL、doctor、projection、JSON closed subset 与 migration 同步闭合 | 本地关闭 |
| KAFA-P1-3 | 合法 phase/scope 只能由内部 helper 达到 | 公开 `baseline confirm` 与 `delivery ready` 闭合手工 journey，不恢复任意 phase mutator | 本地关闭 |
| KAFA-P1-4 | execution 环境 provenance 不完整 | target digest、OS/runtime/binary/policy、container engine/image digest 均不可变且 fail closed | 本地关闭；不声称外部 cryptographic provenance |
| KAFA-P2-1 | live Codex 报告绑定旧 dirty source | single/parallel 报告绑定上述 exact executable candidate 与同一 binary，consistency errors 为空 | 当前候选关闭；clean committed HEAD not-run |
| KAFA-P2-2 | 无 LICENSE/SBOM/provenance/main protection | MIT LICENSE、2 SBOM、checksum、local provenance、no-publish rehearsal、main protection 已验证 | 本地关闭；远端 attestation/release not-run |
| KAFA-P2-3 | 只有 integrity counters，没有 outcome contract | 六项 versioned metrics 与固定 4-case before/after benchmark 已实现 | regression closure；field window not-run |

## Schema 31、迁移与恢复

Schema 31 有 30 张产品表，仅比 schema 30 新增三个本地 authority：

- `acceptance_target_qualifications`；
- `quality_gate_qualifications`；
- `outcome_observations`。

它没有新增 Host task/model/worktree/approval/cancel/handoff 生命周期，也没有
Connector、provider、dispatch、token、network receipt 或第二套 task owner。

Schema 27/28/29/30 均通过 side-by-side schema-31 path。迁移在 operation lock
内重新读取 source，保存 verified DB 与 13 个 projection 的 bytes/existence/mode/
SHA-256，复制有效本地事实，排除 retired 外部事实，且不合成 qualification、gate
review、outcome 或 complete execution provenance。Schema 27/29/30 在 lock 前提交的
decision 与 generation-specific audit event 可被保留；schema 28 本来没有该 authority，
不会伪造。

最终回归还关闭了 legacy finding scope 缺口：当旧 finding 没有 candidate 时，只能
从唯一一致的 evidence 或 gate `candidate_sha/reviewed_commit` 恢复；候选冲突或
cross-cycle gate link 在 activation 前失败。Schema 27 真实 fixture、schema 29
reviewed-commit fallback、existing/evidence conflict 和 cross-cycle probes 均转绿。

任一 conversion、FK/domain/Kernel doctor、projection、activation、cancel、restore
异常都会保持 source 或恢复 verified DB + projection；restore 失败写
`rollback-incomplete` 并保留双重错误。普通 `record_delivery()` 的 projection
Low limitation 单独见“剩余风险”。所有迁移均发生在临时 fixture/HOME；没有业务
项目数据库被迁移。

## 最终验证记账

| Gate | 精确结果 | 非通过项 |
| --- | --- | --- |
| 高风险 targeted union | 770 total；756 pass；0 failure/error | 14 skip，不计 pass |
| 完整 `unittest discover` | 776 total；762 pass；0 failure/error；0 expected-failure；389.342 s | 14 skip，不计 pass |
| Artifact-backed LICENSE | 3/3 pass | 无 |
| Runtime smoke | 2/2 pass | 无 |
| Skill transcript | 22/22 required markers | marker evidence，不是 Native evidence |
| Fixture E2E | 6/6 pass | fixture-only |
| Stability E2E | 11/11 pass | deterministic local profile |
| Outcome benchmark | historical before 4/4 false-delivery；current after 4/4 fail-closed；closure 1.0 | field metrics 全部 not-run/null |
| Native single / parallel | 各 1/1 pass；consistency errors 各 0 | clean committed evidence not-run |
| Schema contract | 13/13 pass；18 schemas | 无 |
| Structure / JSON / docs | Plugin structure valid；全部 repository JSON `jq empty`；documentation/feature-freeze tests 进入 770 union | 无 |
| OpenSpec pre-archive | 4/4 artifacts；validation pass | archive 见后续补记 |

14 个 discovery skips 的构成是 12 个 Windows-only path/handle/junction contracts
和 2 个需要 `KAFA_TEST_WHEEL/KAFA_TEST_SDIST` 的 artifact license cases。后两者在
真实制品环境中作为 3/3 suite 通过；原 discovery skips 仍保持 skip。当前 macOS
real container capability 可用并实际执行，没有把历史 capability skip 冒充通过。

第一次 discovery 命令因 zsh 展开未引用的 `test_*.py` 而未启动任何测试；修正为
`-p 'test_*.py'` 后，上述 776-case run 才是权威结果。最终回归中较早的一次
770 前身曾真实暴露 fixture blocker-code 与 legacy finding candidate 缺陷；修复后
才取得当前 clean accounting。

## Native Codex E2E

用户明确同意把合成提示和临时测试文件发送到 ChatGPT.com。最终报告：

| Profile | Result | Tokens | Runtime | Parallel overlap | Report SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- |
| single | 1/1 | 51,983 | 40.184665 s | n/a | `612b66173176b56f6d10f1aeaa4e626633437bb10ec1fdde784d3dd7c5f56337` |
| parallel | 1/1 | 131,893 | 37.822439 s | 36.639224 s | `9cc47cc04e7a5475d923b8cb504d8508c084af9ce57205eb72e7c2cb3c7d1211` |

Single 只改变 producer workspace 的 `candidate.py`；parallel 两个 producer 只改变
`alpha.py` / `beta.py`，无 scope overlap，targeted 与 combined controller verify
均为 0。报告证明当前本地 Native capability、隔离、scope、token 与 timing 记账，
不证明 semantic qualification、独立 cryptographic identity 或 clean release。

## 制品、供应链与隔离安装

Pinned tooling 由 `release-tooling.json` 锁定并实际读回：

- `build==1.5.0`；
- `setuptools==83.0.0`；
- Syft `1.48.0` / commit
  `3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6`；
- Codex CLI `0.143.0`。

最终持久制品与证据位于 `/private/tmp/kafa-final-dist-f2f952f7/`：

| Subject | Bytes | SHA-256 | CycloneDX SHA-256 |
| --- | ---: | --- | --- |
| wheel | 46,640 | `9f22bd8de0ea33ca76a4cf18f91a2e3f339cf9f496f1b14703f816d57b3bac6b` | `971f1a7c3efbb2adc341158396f588365b666491f48d86de6b2ac6ae884b07a6` |
| sdist | 498,645 | `444ee1c3a6328289ce0d576880a84ca284081d08fc62be3578b2c28418bbbddf` | `a5b180fc7a042252f49618dd451f20f8e5af2b4d1d4690ea9cd7b33d55f9306b` |

`SHA256SUMS`、两份 CycloneDX、
`kafa-build-provenance.intoto.json` 与
`kafa-supply-chain-manifest.json` 独立 verify；assurance 明确是
`unsigned-local-integrity-statement`。该 build-time source snapshot 是
`3f346c6fc0546938485827b3ffdcef036e627200aa1fec57c9e7265f8dd5db33`
（207 files）；后续 audit/OpenSpec checkbox 属 evidence-only closure，不把它们
伪装成重新构建过的 artifact source。

No-publish rehearsal 报告
`docs/runtime/release-rehearsal.json`（SHA-256
`3b5b22605b426f66a98787f02087bc7d020313fcc7d16d5bff67ac9ab6b3e15c`）
完成了 snapshot → build → 2 SBOM → verify → isolated install → verify。真实
wheel/sdist 的第二次独立 smoke 又完成 venv import、marketplace/app-server discovery、
cache/source/managed tree digest、7 Skills、3 Hooks、3 templates、18 schemas、7 runtime
scripts、quickstart、schema30→31 migration backup/dry-run/doctor、direct hook handler、
unregister、cache/marketplace/plugin removal 与 full uninstall。

用户安装前后均为 `kafa 2.0.0-beta.1`，
`codex-project-harness@personal 2.0.0-beta.1` installed/enabled，路径未变。

## Before / After 指标

| 指标 | Baseline | Final | 变化 / 判定 |
| --- | ---: | ---: | --- |
| Python files | 66 | 81 | +15 / +22.727% |
| Python total LOC | 51,725 | 71,633 | +19,908 / +38.488% |
| Plugin Python LOC | 25,503 | 33,082 | +7,579 / +29.718% |
| Test Python LOC | 24,149 | 34,620 | +10,471 / +43.360% |
| `kafa/` LOC | 1,762 | 3,274 | +1,512 / +85.812% |
| Benchmark LOC | 311 | 657 | +346 / +111.254% |
| Product tables | 27 | 30 | +3，锁定 schema-31 authority |
| Public JSON schemas | 16 | 18 | +2 |
| CLI parser nodes | 53 | 59 | +6 |
| Skills / Hooks / templates | 7 / 3 / 3 | 7 / 3 / 3 | 不变 |
| Fresh DB | 315,392 B | 380,928 B | +65,536 / +20.779%；超旧 320 KiB 53,248 B |
| Plugin payload | 1,044,089 B | 1,333,527 B / 71 files | +289,438 / +27.722%；超旧 1 MiB 284,951 B |
| Wheel | 30,030 B | 46,640 B | +16,610 / +55.311% |
| sdist | 370,134 B | 498,645 B | +128,511 / +34.718% |

旧 baseline 的 test LOC `23,774` 是转录错误；由冻结 HEAD blob 复算后应为
24,149，才能与 total LOC 51,725 对账。用户此前接受 LOC deviation，但本文不把
它描述为 slimming metric 通过。DB/plugin 的新尺寸也没有通过旧预算，只作为明确
偏差保留。

三组独立 5-sample 性能中位数：

| Metric | Final medians | Baseline representative | 结论 |
| --- | --- | ---: | --- |
| Init | 204.526 / 206.390 / 198.725 ms | 159.367 ms | +28.336%，比较项 |
| 5k mutation | 29.145 / 28.617 / 35.625 ms | 17.853 ms | 三组均通过唯一硬门槛 ≤50 ms；最差余量 14.375 ms |
| Targeted 3-view | 21.348 / 21.400 / 22.256 ms | 13.683 ms | +56.398%，比较项 |
| Full 13-view | 98.623 / 94.675 / 97.176 ms | 67.490 ms | +43.986%，比较项 |

新增可比较测量：cold CLI help 92.556 ms、initialized status 134.020 ms、5k
public projection rebuild 199.001 ms、warm delivery evaluator 24.672 ms、cold
`validate --delivery` 284.373 ms。除 mutation 外没有既定 numeric gate，因此不标
“通过预算”。原始报告：`/private/tmp/kafa-11-6-final-exclusive-{1,2,3}.json`。

Outcome regression report
`docs/runtime/delivery-integrity-outcome-benchmark.json`（SHA-256
`c23e589ec1c809039810b541ddbfbfeeb584cfc638961485574cc595233f9b83`）
绑定 executable candidate `f2f952f7…`，固定 before 4/4 false-delivery 与 after
4/4 fail-closed。六项 field metric 均为 `not-run/null`，
`field_improvement_claimed=false`。

## 独立 QA 与对抗审查

QA A 覆盖 minimum graph、qualification、cancelled/accepted task、schema31、
schema27-30 migration、operation lock、backup/rollback/projection；QA B 覆盖
medium/high trust、shared readiness、execution provenance、public journey、Native、
outcome 和 supply-chain。早期 QA 暴露的 High/Medium 均由 main 修复后复审：

- historical audit 必须重放 delivery-time policy/trust、baseline revision、ordered
  confirmation/gate/delivery event chain、cycle event digest 和 cycle invariants；
- accepted task 必须有 evidence 与 actor/event，伪造 post-delivery event 或跨 cycle
  fact 不能改变历史结论；
- schema27/29/30 decision 与 generation-specific event 必须在 lock 前兼容；
- legacy finding candidate 必须复用 gate `candidate_sha or reviewed_commit`，
  conflict/cross-cycle 必须 fail closed；
- deterministic E2E 统计必须匹配 stable blocker code，不能因文案变化漏记阻断。

最终 bounded QA A：11/11 formal tests + 2/2 adversarial probes，C/H/M=0；QA B
第二轮：32/32，C/H/M=0。QA A 的更广 migration/graph checkpoint 为 146/146。
所有主修复都进入最终 770 targeted 与 776 discovery；子 agent 没有修改 production。
未调用 Codex Security。

## 剩余风险与 not-run

已知 Low：普通 `record_delivery()` 先提交 DB，再生成 projection；若注入 render
failure，DB 可已 delivered 而 projection 暂时陈旧。`doctor` 会 fail closed，
`render_all` 可修复；migration 的 DB + projection 原子 rollback 不受此限制。本
change 不为该 Low 引入新的 schema/lifecycle，后续可单独 harden。

Qualification、reviewer context 与 risk acceptance 仍是可审计的 procedural
metadata，不是自动语义证明或外部身份签名。High/critical 缺少独立 current review
仍返回 `human-review-required`。

以下项目保持 `not-run`，不得描述为发布通过：

- commit、push、merge、PR 与当前 candidate 的远端 Ubuntu/macOS/Windows CI；
- clean committed reviewed HEAD；
- GitHub 对未 push LICENSE 的 license detection；
- GitHub build attestation、tag、release、publish、deploy；
- live installed Host hook turn；
- field observation window 与任何 field improvement claim；
- 生产/业务项目 schema migration；
- 用户级 Kafa/plugin replacement。

Main branch protection 已按单独授权启用：PR、1 approving review、conversation
resolution、strict Ubuntu/macOS/Windows required checks、admin enforcement；force
push/delete disabled。仓库当前只有一名 eligible collaborator 时，1-review 规则可能
阻塞合并，需未来增加 reviewer 或明确调整规则。

## OpenSpec Archive

Pre-archive status 为 4/4 artifacts complete，`openspec validate
delivery-integrity-hardening` passed。仅在 tasks 12.1–12.6 全部写入证据后执行
`openspec archive delivery-integrity-hardening -y --json`，成功归档为
`2026-07-21-delivery-integrity-hardening`，canonical spec 更新统计为 added 11、
modified 1、removed 2、renamed 0，且 active changes 变为 0。

归档后将旧 requirement 中仍把 schema 30 写成当前激活目标的 16 处表述更新为
schema 31；保留 schema 30 作为受支持迁移来源、历史 `active` failure-mode
转换来源和 27-table predecessor 的 4 处事实。Post-archive 验证结果：

- `openspec validate --all`：1/1 spec passed；
- documentation/release/schema-31/feature-freeze：60/60 passed；
- Plugin structure：valid；repository JSON：全部 `jq empty`；
- Native single/parallel 对当前 executable source 的 consistency errors：各 0；
- evaluation identity 仍为 workspace `f2f952f7...`、status `41fcfbd1...`、
  HEAD `e3d46d9f...`、87 entries；
- `git diff --check`：passed。

一次从仓库根运行 structure script 的调用因其按 CWD 查找
`.codex-plugin/plugin.json` 而退出 1；该误调用没有执行结构测试，也不计入通过或
产品失败。按脚本实际路径契约从 Plugin 根重跑后通过。

## 最终决定

149/149 checklist items 均有当前证据，OpenSpec archive 与 post-archive
validation 已完成；Codex Brain capture receipt 为 `5a81efb764971e5bd4fb`，仓库内
测试/构建缓存已清理。P0/P1/P2 的本地实现与验收判定关闭，当前目标可以完成。

该结论不授予 release，也不把 14 个 skips、远端 CI/commit/push/merge、field
metrics、生产迁移或用户安装替换描述为通过。普通 `record_delivery()` 在 DB commit
后 projection render 失败可能留下 doctor 可检测且 `render_all` 可修复的 stale
projection，作为 Low residual 保留；DB/plugin 旧 size budget 仍未通过。

# Goal A Stop-ship 失败回归基线

## 状态

- 分支：`v1.26-stop-ship-correctness`
- 审计基线提交：`e80f46e Document systemic stop-ship audit`
- 生产实现：未修改
- 新增红测：`tests/test_stop_ship_regressions.py`
- 预期状态：六项测试在当前实现上全部失败；它们是后续修复的最小负向基线，不是完整退出矩阵，也不是当前 release gate 的通过证明

## 基线证明

添加红测前执行：

```text
python3 -m unittest tests/test_delivery_cycles.py tests/test_cold_start_guided_loop.py tests/test_session_attestation.py tests/test_install_release.py
Ran 29 tests in 27.156s
OK
```

添加红测后执行：

```text
python3 -m unittest -v tests/test_stop_ship_regressions.py
Ran 6 tests in 9.123s
FAILED (failures=6)
```

稳定性复验连续执行两次，分别在 `7.712s` 和 `7.660s` 结束，两次均为 `FAILED (failures=6)`。完整测试发现结果：

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
Ran 248 tests in 410.981s
FAILED (failures=6)
```

主 agent 对当前分支执行的完整回归没有出现第七个失败；六项失败全部来自本文件列出的 stop-ship regressions。该结论不是独立环境或真实宿主 compatibility 证明。

首轮 `IN-001` 曾因使用不存在的 `python -m kafa` 入口而失败。测试入口改为 `python -m kafa.cli` 后，失败稳定落在 marketplace source path 不一致，而不是测试环境错误。

## 六项失败证据

| ID | 目标行为 | 当前确定性实际行为 | 红测 |
| --- | --- | --- | --- |
| DT-001 | passing gate 关联 open critical finding 时阻断 delivery | finding 已以 `critical/open` 关联 gate，但 `delivery_readiness` 返回 `0` | `test_dt_001_open_critical_finding_blocks_delivery` |
| DT-002 | 同秒 pass 后写 fail 时，后写 fail 必须成为 latest | 已证明 SQLite `rowid` 顺序为 `pass, fail`，但人为固定同秒和 ID 后 readiness 返回 `0` | `test_dt_002_same_second_newer_fail_gate_wins` |
| CY-001 | 新旧 cycle 可分别拥有本地 `R1/AC1/T1` | requirement/acceptance 旧 row 被覆盖并搬迁；task 则拒绝在新 cycle 重用同一 ID | `test_cy_001_reusing_local_ids_does_not_move_old_cycle_history` |
| TR-001 | 普通 Kernel CLI 无外部 receipt 时不得获得 connector trust | 仅设置 `HARNESS_CONNECTOR_KEY`、不提供 token，CLI 仍返回 `0` 并写 connector-trusted attestation | `test_tr_001_cli_cannot_self_issue_connector_attestation` |
| QS-001 | quickstart 执行后停在待独立 QA 状态 | 实际 tuple 为 `('delivered', 1 delivery, 1 fresh pass gate, no reviewer next step)` | `test_qs_001_quickstart_stops_before_independent_qa_and_delivery` |
| IN-001 | user marketplace source 必须解析到安装器复制目录 | source 解析为 `$HOME/codex-project-harness`，复制目录为 `$HOME/.agents/plugins/codex-project-harness` | `test_in_001_user_marketplace_source_resolves_to_copied_plugin` |

## 根因假设与修复依赖

### DT-001

- 涉及文件：`core/gate_engine.py`、`scripts/harness_db.py`。
- 根因假设：readiness 只读取 `quality_gates.blocking_findings` 文本，不 join `quality_gate_findings -> findings`。
- 后续依赖：schema 29 必须先定义 finding scope、candidate/revision 和 waiver lifecycle，避免只补一个全局查询。
- 建议批次：Wave 2，在 gate 全序完成后修复。

### DT-002

- 涉及文件：`scripts/harness_lib.py`、`scripts/harness_db.py`、`core/gate_engine.py`。
- 根因假设：秒级 `created_at` 加随机 UUID 不能表达写入全序。
- 后续依赖：schema 29 增加数据库分配的 gate sequence/revision 和 supersede 关系。
- 建议批次：Wave 1 设计 migration，Wave 2 首项实施。

### CY-001

- 涉及文件：`scripts/harness_db.py` 及 requirement/acceptance/task/failure-mode schema。
- 根因假设：业务本地 ID 同时被当作全局主键，upsert 会更新 `cycle_id`。
- 后续依赖：schema 29 选择不可变内部 ID，或以 `(cycle_id, local_id)` 为唯一身份，并迁移所有 link foreign key。
- 建议批次：Wave 1 完成无损迁移设计，Wave 2 更新 same-cycle query。

### TR-001

- 涉及文件：`core/connector_trust.py`、`scripts/harness.py`、attestation/CI verification 写入路径。
- 根因假设：`prepare_connector_record()` 同时承担 issuer 与 verifier，空 token 会由本进程签发。
- 后续依赖：先形成 external issuer ADR，并为 schema 29 定义 `legacy-untrusted` 或等价迁移状态。
- 建议批次：Wave 2；不能用隐藏 CLI flag 或同进程 broker 冒充信任分离。

### QS-001

- 涉及文件：`scripts/harness_db.py` 的 `quickstart_minimal()`，以及 quickstart 文档/输出。
- 根因假设：便利流程在同一进程同时扮演 producer、reviewer 和 delivery recorder。
- 后续依赖：TR-001 和独立 reviewer receipt 语义先明确；quickstart 只保留 setup、controller test 和 next step。
- 建议批次：Wave 2 最后修复。

### IN-001

- 涉及文件：`kafa/cli.py`、marketplace 安装测试和发布安装矩阵。
- 根因假设：user install 的复制目标与 Codex 对 local source path 的解析基准不一致。
- 后续依赖：确定 canonical marketplace layout，并在隔离 HOME 做真实 `marketplace add -> plugin add -> plugin list` compatibility E2E。
- 建议批次：Wave 3；当前红测只证明本地路径契约，不宣称真实 Codex 安装已经验证。

## 正在固化错误行为的既有测试

以下测试暂不修改，等对应实现修复时一起翻转：

- `tests/test_cold_start_guided_loop.py::test_quickstart_minimal_execute_reaches_delivered_cycle` 明确期待 quickstart 自动 delivered。
- `tests/test_session_attestation.py::test_connector_with_key_generates_valid_session_hmac` 明确期待普通 CLI 空 token 自签。
- `tests/test_harness_operating_system.py` 的 critical-risk connector trust happy path 依赖同进程签发。
- `tests/test_harness_operating_system.py::test_connector_key_file_path_is_used_without_persisting_key_and_doctor_flags_tracked_key` 同样固化了本地 key-path 由同一进程签发和验证。
- `tests/test_install_release.py::test_user_install_copy_upgrade_and_uninstall` 明确期待错误的 `./codex-project-harness` source path。
- `tests/test_delivery_cycles.py::test_legacy_cycle_validation_and_invalidation_are_audit_only_after_migration` 在新 cycle 改用 `R2/AC2/T2`，因而绕开了 cycle-local ID 重用问题。

DT-001 和 DT-002 没有既有测试明确期待错误结果；当前相关测试只覆盖 finding link 是否存在、单个 pass gate 和单个 fail gate，未覆盖结构化 blocker 派生或同秒全序。

## Goal A 边界

- 不修复实现，不改变 schema，不改变 CLI，不改变 release/version。
- 不把红测加入 expected-failure、skip 或放宽断言；六项失败必须保持对后续修复可见。
- 六项最小红测变绿只证明对应旧行为已消失；resolved finding、waiver expiry、并发 gate winner、migration 无损性、receipt replay 和真实 Codex install 等正反向矩阵仍必须在后续 Wave 补齐。
- Wave 1 开始前需确认 schema 29 identity/sequence migration 和 legacy trust migration 决策。

# Wave 1 Schema Lifecycle 失败基线

## 范围

- 问题：`DB-001`、`DB-002`
- 分支：`v1.26-stop-ship-correctness`
- 测试：`tests/test_schema_lifecycle.py`
- 本切片只建立失败基线，不修改生产实现、schema 或版本。

## 执行结果

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests/test_schema_lifecycle.py
Ran 6 tests in 1.885s
FAILED (failures=6)
```

六项均为目标断言失败，没有 setup error：

| 问题 | 目标契约 | 当前实际行为 |
| --- | --- | --- |
| DB-001 | caller transaction 异常后，调用前写入和 schema DDL 全部 rollback | `caller_fact` 和 `project` 两张表都保留下来 |
| DB-002 actual-from | `--from-version` 必须等于数据库实际版本 | schema 28 数据库接受调用者声明的 `6 -> 28` |
| DB-002 unknown-target | 未注册 target 必须拒绝 | `28 -> 999` 返回成功 |
| DB-002 downgrade | 不支持的降级必须拒绝 | `28 -> 27` 返回成功 |
| DB-002 dry-run | dry-run 必须执行同一 migration path validation | `28 -> 999 --dry-run` 返回成功 |
| DB-002 markdown target | markdown importer 声明的 target 必须等于它实际创建的 schema | `markdown-v1 -> 13 --dry-run` 返回成功，尽管 importer 实际使用当前 schema |

## 根因与修复边界

### DB-001

- `core/store.py::SqliteStore.transaction()` 先执行 `BEGIN IMMEDIATE`，异常时承诺 rollback。
- `scripts/harness_db.py::create_schema()` 使用 `Connection.executescript()`；Python sqlite3 会在 script 前隐式提交 pending transaction。
- 最小修复必须让 schema DDL 逐条运行在 caller transaction 中，或让 Schema Lifecycle 独占连接和事务；不能只在异常后恢复文件来掩盖已破坏的事务语义。
- 同样需要验证 `InMemoryStore`，避免 file-backed 与 memory store 行为分叉。

### DB-002

- `migrate()` 在读取实际 project schema 前处理 dry-run，并直接信任 CLI 的 `from_version/to_version`。
- 当前没有 migration registry，也没有 actual-version CAS、目标版本 allowlist 或 downgrade policy。
- 最小修复应先读取实际版本，再验证 `(actual, requested_from, target)`；dry-run 与 apply 必须复用同一验证函数。
- schema 29 实施前，registry 需要明确支持历史 schema 到当前 schema 的已知路径，未知、错误 from、降级和未注册跳级全部 fail-closed。

## 现有测试影响

- `tests/test_harness_operating_system.py::test_doctor_repair_migrate_and_adapter_records` 在已经 repair/init 为当前 schema 后仍调用 `6 -> 28`，正在固化错误的 caller-authored from-version；实现修复时必须翻转该测试。
- `tests/test_fencing.py::test_schema_14_migration_adds_fence_default_zero`、`tests/test_idempotency.py::test_schema_15_migration_adds_command_log`、`tests/test_sandbox_execution.py::test_schema_21_migration_adds_sandbox_and_integration_audit` 会先把数据库实际版本改成对应历史版本；它们应继续通过注册的历史迁移路径。
- `tests/test_delivery_cycles.py` 的 schema 24 migration 同样应保留为已注册兼容路径。
- 既有 markdown migration 测试传入 `--to-version 13`，却实际创建当前 schema；实现修复时应改为当前受支持 target。

## 后续退出条件

这六项变绿只是最低条件。Wave 1 还必须补充：

- 每个 migration step 的 failure injection 和完整 rollback；
- schema version、表、列、业务 row、link row 和 migration/event row 的前后数量校验；
- backup、dry-run、apply、恢复路径；
- 并发 migration 的确定 winner 或明确冲突；
- schema 28 -> 29 cycle identity、gate sequence 和 legacy trust downgrade 的无损迁移矩阵。

## DB-001 / DB-002 修复证据

本节记录后续实现切片的结果；schema 29 迁移仍未开始。

实现结果：

- `create_schema()` 不再使用会隐式提交的 `executescript()`；SQL 由 `sqlite3.complete_statement()` 划分，并要求调用者已开启事务。
- file-backed 和 in-memory Store 在 schema DDL 后注入异常时均完整 rollback。
- numeric migration 从数据库读取 actual version，CLI `--from-version` 只作为 expected-version CAS。
- 仅注册历史 schema `6..27 -> current 28`；未知目标、降级、错误 source 和错误 markdown target 均 fail-closed。
- current `28 -> 28` 是校验 schema 后的 projection rebuild；缺表不能伪装成健康 no-op。
- numeric 和 markdown migration 都具备并发单 winner / 幂等恢复语义，不再由 loser 恢复旧文件覆盖 winner。
- DB 已提交但 projection 失败时返回明确恢复命令；重试不会重复业务 row 或 migration marker。
- checkpoint restore 使用 deferred FK，并覆盖 `task_test_targets` 关系 round-trip。
- event replay 显式开启事务；失败 rollback 并删除部分输出数据库。
- WAL 初始化锁遵守五秒总 deadline；stability contention 未泄漏 `database is locked`。

定向矩阵：

```text
python3 -m unittest tests/test_schema_lifecycle.py tests/test_store_seam.py
Ran 21 tests in 3.154s
OK

历史 schema 14/15/21/24、markdown、checkpoint 和 repair 定向矩阵
Ran 27 tests in 20.119s
OK
```

Stability E2E：

```text
scenario_count=12
failed_count=0
false_pass_count=0
sqlite_lock_error_count=0
human_intervention_count=0
sqlite_contention_stress.pass=true
doctor_returncode=0
invariant_returncode=0
```

最终完整回归：

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
Ran 265 tests in 433.259s
FAILED (failures=6)
```

六个失败全部是 `test_stop_ship_regressions.py` 中尚待 Wave 2/3 修复的 P0 基线；DB-001/DB-002 和普通回归没有额外失败。两轮独立对抗审查发现的 checkpoint FK、event replay、numeric/markdown concurrency、no-op corruption 和 projection recovery 缺口均已增加测试并关闭。

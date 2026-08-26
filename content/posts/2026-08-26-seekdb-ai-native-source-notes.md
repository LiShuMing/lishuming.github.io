---
title: "【源码】seekdb：AI-Native 数据库的 Change Stream、双层 HNSW 与 COW Sandbox"
slug: "seekdb-ai-native-source-notes"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
categories:
  - Database Engineering
tags:
  - seekdb
  - OceanBase
  - AI-Native Database
  - Vector Search
  - HNSW
  - Change Stream
  - Hybrid Search
  - Copy-on-Write
description: "基于 seekdb develop 分支源码，分析 Change Stream 异步索引、增量与快照双层 HNSW、统一 Hybrid Search、FORK/MERGE COW Sandbox、Embedded 与 AI Service，并讨论 AI-Native 数据库真正需要原生什么。"
draft: false
toc: true
math: false
---

过去两年，几乎每个数据系统都在给自己加上 AI 标签：支持 vector type、接入 embedding model、提供一个 RAG demo，似乎就成了 AI database。但如果把 marketing slide 拿掉，一个真正面向 LLM 和 Agent 工作负载设计的数据库，究竟应该在哪些地方与传统 OLTP、OLAP 或独立 Vector DB 不同？如果今天从头设计一款 engine，哪些能力应该进入内核，而不是继续由应用胶水拼起来？

这次我选择阅读 [seekdb](https://github.com/oceanbase/seekdb) 源码。本文基于本地 `develop` 分支的 `v1.1.0-572-g1e9b720c`，提交为 [`1e9b720c`](https://github.com/oceanbase/seekdb/commit/1e9b720c)。seekdb 的定位是 “The State Store for AI Agents”，主打流式写入与检索、向量/全文/标量混合搜索、可写 COW Sandbox，以及 Embedded/Server 两种形态。

读完源码后，我的核心结论是：

> seekdb 不是从空白开始重写的 Vector Engine。它的 AI-Native 价值，恰恰来自把 freshness、关系/全文/向量检索、可写沙箱和嵌入式部署，整合进 OceanBase 演化而来的 SQL、事务、日志、Tablet、MemTable/SSTable 与 Compaction 内核中。

这也意味着本文不会把 README 的每一句宣传语直接当作实现结论。比如异步索引不等于提交后零延迟可见；“两层 HNSW”背后还有辅助表、删除 bitmap 和 refresh/rebuild；`FORK` 并不保证每次都是纯 metadata 操作；`MERGE TABLE` 更不是 Git 式三方合并。真正值得学习的是这些边界如何在源码中落地。

## 1. AI-Native 到底应该 Native 什么

LLM 本身是无状态的。一个生产级 Agent 则至少同时维护四类状态：

| 状态 | 典型内容 | 数据系统需要保证什么 |
| --- | --- | --- |
| Working state | 当前任务、工具调用、计划、checkpoint | 低延迟读写、事务、失败恢复 |
| Episodic memory | 对话、事件、历史执行轨迹 | 时间过滤、全文/语义检索、持续写入 |
| Semantic memory | 文档 chunk、实体、embedding | ANN、metadata filter、freshness |
| Procedural state | prompt、workflow、policy、代码版本 | 结构化查询、版本隔离、审计与回滚 |

只提供 Vector Search，最多解决了 semantic memory 的近邻召回。真实 Agent 还会提出如下要求：

1. 写入一条新记忆后，很快就能被检索到，同时不能让在线建索引拖垮写入 P99；
2. 一次查询同时包含 tenant、时间、权限等 scalar filter，以及全文关键词和 vector similarity；
3. Agent 可以在隔离环境中大胆修改状态，验证后选择接受或丢弃；
4. 本地应用可以零运维嵌入，服务端又能沿用 SQL 和事务语义；
5. embedding、rerank、completion 是可治理的模型调用，而不是散落在每个应用里的 HTTP glue code。

因此我更愿意把 AI-Native 定义为：**数据库内核是否把 AI workload 的状态生命周期当成第一等问题**。Vector datatype 只是必要条件之一；新鲜度协议、混合检索计划、状态分支、模型 I/O 和资源隔离同样重要。

## 2. 先看清底座：它仍是一套完整数据库内核

seekdb 的目录结构已经给出了答案：

```text
src/
├── sql/                         parser / resolver / optimizer / executor
│   └── hybrid_search/           JSON request -> SQL
├── storage/
│   ├── memtable/ block_sstable/ compaction/
│   ├── tx/ tablet/
│   ├── vector_index/            vector index refresh/rebuild
│   └── fts/                     full-text index
├── logservice/palf/             redo log
├── observer/
│   ├── change_stream/           log-fed async index pipeline
│   ├── vector_index/            HNSW adaptor / embedding handler
│   ├── ai_service/              endpoint metadata and execution
│   └── embed/                   C / Python / Android embedding APIs
└── rootserver/fork_table/        FORK TABLE / DATABASE DDL
```

从一次 SQL 写入看，seekdb 仍然拥有传统数据库的完整链路：parser/resolver 产生 statement，optimizer/code generator 生成执行计划，事务层写 redo，MemTable 接收版本，后续 freeze、SSTable 和 compaction 管理持久数据。向量索引不是绕过事务旁路写入的另一份真相，而是基于已提交 redo 异步派生出来的索引状态。

这与“从头写一个专用 ANN server”是两种路线。后者可以把单一向量 workload 做到非常窄和快；seekdb 选择复用成熟关系数据库基础，把 Agent 所需的结构化状态、事务、SQL、全文索引和 Vector Search 放进一个一致性域。代价也显而易见：二进制、启动过程和内部组件都不会像一个小型向量库那样轻。

源码入口可以先记住这张表：

| 问题 | 主要源码 |
| --- | --- |
| Change Stream | [`src/observer/change_stream/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/change_stream) |
| HNSW 查询与合并 | [`ob_plugin_vector_index_adaptor.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_adaptor.cpp) |
| Vector refresh/rebuild | [`src/storage/vector_index/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/storage/vector_index) |
| Hybrid Search | [`src/sql/hybrid_search/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/sql/hybrid_search) |
| FORK | [`src/rootserver/fork_table/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/rootserver/fork_table)、[`ob_tablet_fork_task.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/ddl/ob_tablet_fork_task.cpp) |
| MERGE TABLE | [`ob_merge_table_resolver.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/cmd/ob_merge_table_resolver.cpp)、[`ob_merge_table_executor.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/cmd/ob_merge_table_executor.cpp) |
| Embedded API | [`src/observer/embed/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/embed) |
| AI model/embedding | [`src/observer/ai_service/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/ai_service)、[`ob_expr_ai_embed.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/expr/ob_expr_ai/ob_expr_ai_embed.cpp) |

### 2.1 一张图看懂运行时组件

seekdb 虽然主要面向单机和 Embedded 场景，但源码仍保留了 OceanBase 的 LS（Log Stream）与 Tablet 抽象。理解这些名字后，后面的代码会清楚很多：

```text
                         seekdb process
┌──────────────────────────────────────────────────────────────────────┐
│ 接入层                                                               │
│ MySQL protocol -> ObMPQuery       Embedded -> ObInnerSQLConnection   │
│                       └──────────────┬───────────────┘                │
│                                      ▼                               │
│ SQL 层                                                               │
│ ObSql -> Parser -> Resolver -> Optimizer -> CodeGen -> Operator/DAS  │
│                                      │                               │
│               ┌──────────────────────┴──────────────────────┐        │
│               ▼                                             ▼        │
│ 主数据写路径                                      Vector/FTS 查询路径 │
│ ObDASInsertOp                                     ObDASHNSWScanIter  │
│ -> ObAccessService                                -> VSAG adaptor    │
│ -> LS/Tablet                                      -> aux-table lookup│
│ -> MemTable MVCC                                                    │
│               │ redo callback                                        │
│               ▼                                                       │
│ Transaction / Palf log                                                │
│               │ committed redo                                        │
│               ▼                                                       │
│ Change Stream: Fetcher -> Dispatcher -> Worker -> AsyncIndex plugin   │
│                                                      │                │
│                                  index-id/delta tables + incr HNSW     │
│                                                                       │
│ DDL/状态分支：RootService -> Fork DDL task -> per-tablet fork DAG     │
└──────────────────────────────────────────────────────────────────────┘
```

这张图中有三类状态不能混淆：

- **primary state**：用户表的 MVCC row、MemTable/SSTable 与事务 redo，是事实来源；
- **derived state**：HNSW、FTS 以及辅助映射表，是可重建但必须满足可见性协议的派生状态；
- **control state**：schema、DDL task、refresh SCN、snapshot lease，决定两者怎样切换和恢复。

AI-Native 能力不是另一个旁路 Server，而是在这三类状态之间增加协议。

### 2.2 一次普通 INSERT 到 redo 的源码路径

先不看向量索引，只跟踪最普通的一次 `INSERT`。Server 模式由 [`ObMPQuery::do_process()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/mysql/obmp_query.cpp) 调用 `sql_engine_->stmt_query()`；Embedded 模式由 `ObLiteEmbedConn::execute()` 调用 inner SQL connection。两者最终都进入 [`ObSql::handle_text_query()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/ob_sql.cpp)，经过 plan cache、parser、resolver、optimizer、code generation 和物理算子。

写入存储的调用链可以压缩成：

```text
INSERT operator
  -> ObDASInsertOp::open_op()
       -> ObDASIndexDMLAdaptor::write_rows()
            -> ObAccessService::insert_rows(ls_id, tablet_id, tx_desc, ...)
                 -> ObLSTabletService::insert_rows()
                      -> ObTablet::insert_rows()
                           -> prepare_memtable()
                           -> ObMemtable::set() / multi_set()
                                -> mvcc_write_()
                                -> check frozen-store conflicts
                                -> register_row_commit_cb()
                                -> finish_kv(s)
```

[`ObDASInsertOp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/ob_das_insert_op.cpp) 是 SQL execution 与 storage access 的分界。去掉错误处理后，它做的事情非常直接：

```cpp
// SQL/DAS 已经确定目标 LS、Tablet、列和 transaction descriptor。
ObAccessService *as = share::g_mp->access_service();
as->insert_rows(ls_id,
                tablet_id,
                *tx_desc_,       // 当前事务，不是一次独立的 index write
                dml_param_,
                ctdef.column_ids_,
                &iter,
                affected_rows);
```

[`ObAccessService::insert_rows()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/tx_storage/ob_access_service.cpp) 先通过 `check_write_allowed_()` 获取并校验 LS、Tablet 与 write store context，再交给 `ObLSTabletService`。[`ObTablet::insert_rows()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/tablet/ob_tablet.cpp) 保护 active MemTable，最后选择单行 `set()` 或批量 `multi_set()`：

```cpp
guard.refresh_and_protect_memtable_for_write(relative_table);
prepare_memtable(relative_table, store_ctx, write_memtable);

if (row_count == 1) {
  write_memtable->set(param, context, arg);
} else {
  write_memtable->multi_set(param, context, arg, rows_info);
}
```

真正的事务语义在 [`ObMemtable::set_()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/memtable/ob_memtable.cpp) 中完成。下面是按源码步骤整理的等价骨架：

```cpp
build_row_data_(..., old_row_data, new_memtable_data); // 序列化 old/new row
mvcc_write_(ctx, memtable_key, tx_node, ...);          // 插入未提交 MVCC node
check_row_locked_on_frozen_stores_(...);               // 检查写写冲突/重复键

mem_ctx->register_row_commit_cb(...);                  // 注册 redo + commit/abort callback
mvcc_engine_.finish_kv(mvcc_result);                   // 完成内存节点链接
```

`register_row_commit_cb()` 是理解 Change Stream 输入的关键。MemTable 并非当场手写一份独立 CDC event，而是注册 `ObMvccRowCallback`；事务提交前，`ObTransCallbackMgr::fill_from_all_list()` 遍历 callback，`ObMvccRowCallback::get_redo()` 把 key、old row、new row、DML flag、sequence number 和 tablet ID 填入 `RedoDataNode`。随后 transaction/log service 把 mutator 写入 Palf。

因此主写路径与异步索引的边界非常清楚：

```text
MemTable MVCC node + row callback
          │
          ├── commit/abort 决定主表可见性
          └── redo mutator 成为 Change Stream 的唯一事实输入
```

Change Stream 不读取 SQL 文本，也不依赖客户端重复发送 vector；它消费的是存储层已经序列化并进入事务日志的 old/new row。这使 `UPDATE`、`DELETE`、rollback 和 crash recovery 能共享同一套事务事实。

## 3. Change Stream：把提交路径与索引路径拆开

Agent memory 的典型 workload 不是离线 bulk load 后只读，而是不断写入 conversation、tool result 和新文档，同时持续搜索。若每次 DML 都同步修改一个复杂 HNSW graph，写延迟会被图更新、锁竞争和内存分配放大。seekdb 的关键选择是：**事务提交只负责主存储和 redo，异步 Change Stream 再把已提交变化送进增量索引。**

源码中的主链路如下：

```text
Palf / CLOG
    │
    ▼
ObCSFetcher
    按 tx_id 聚合 redo
    处理 commit / abort / rollback-to-savepoint
    维护 change_stream_min_dep_lsn
    │ committed transactions
    ▼
ObCSDispatcher
    redo -> ObCSRow(old row / new row / commit version)
    按 schema_version 成批，按 heap_pk 切 subtask
    │
    ▼
ObCSWorker / ObCSExecutor
    并行执行 index plugin
    │
    ▼
ObCSPluginAsyncIndex
    insert/delete event -> auxiliary tables + delta HNSW
    │
    ▼
按 batch_sn 串行提交
    advance change_stream_refresh_scn
```

### 3.1 Fetcher 不是简单地 tail WAL

[`ObCSFetcher`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_fetcher.cpp) 读取 Palf log 后，用 `ObCSTxInfo` 按 transaction ID 保存多段 redo。事务在提交前可能跨多条日志，可能 abort，也可能 `ROLLBACK TO SAVEPOINT`。后者不会删除已经写出的 redo，因此 `ObCSTxInfo` 还记录 rollback range，后续通过 `is_row_visible()` 排除已回滚 statement。

`ObCSTxInfo` 的字段本身就是这段协议的最小状态机：

```cpp
struct ObCSTxInfo {
  int64_t tx_id_;                    // 聚合 key
  int64_t commit_version_;           // commit 后才赋值
  palf::LSN start_lsn_;              // 日志回收下界
  int64_t schema_version_;           // 解析 row 时使用的 schema
  ObArray<ObCSRollbackRange> rollback_list_;
  ObArray<ObCSRedoRecord> redo_list_;
};
```

四类 transaction log 的处理行为不同：

```cpp
handle_redo_log_:
  get_or_create_tx_info_(tx_id, lsn, tx);
  tx->redo_list_.push_back(copy(mutator));

handle_rollback_to_log_:
  tx->rollback_list_.push_back({to, from});

handle_abort_log_:
  tx_info_.erase(tx_id);              // 整个事务不再产生派生状态

handle_commit_log_:
  tx->commit_version_ = commit_scn;
  tx->schema_version_ = current_schema_version_;
  dispatcher_->push(tx);              // 到 commit 才进入下游
```

这里的 `copy(mutator)` 也解释了一个资源风险：长事务会让 Fetcher 在内存中保留多段 redo；同时 `start_lsn_` 又阻止对应日志回收。异步索引落后不只表现为“搜索旧一点”，还会转化为 Change Stream memory 与 log-disk pressure。

这说明 CDC/异步索引的输入不是“看见一条 row redo 就应用一条”。正确边界必须是 committed transaction，并复现 savepoint visibility。否则主表已经回滚的向量仍会留在索引中。

Fetcher 还推进 `change_stream_min_dep_lsn`。checkpoint/log recycle 会读取这个位置，避免 Change Stream 尚未消费的 redo 被提前回收。异步消费者因此反过来约束日志保留：解耦了前台延迟，却没有消除后台积压的存储成本。

### 3.2 并行执行，串行提交

[`ObCSDispatcher`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_dispatcher.cpp) 把 committed redo 解析成 old/new row，按 schema version 组织 batch，再按 key 拆成多个并行 subtask。相同 batch 可以并发构建索引事件，但 [`ObCSWorker`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_worker.cpp) 必须等待 `next_commit_sn_`，按顺序提交 batch。

Dispatcher 的 `do_dispatch_()` 有三个不容易从架构图看出的约束：

```cpp
exec_ctx->batch_sn_ = dispatch_sn_;       // batch 的日志顺序起点
exec_ctx->epoch_ = dispatcher_epoch_;     // 失败恢复代次

while (row_count < CS_AGGREGATE_ROW_THRESHOLD) {
  tx = tx_ring_.get(dispatch_sn_);
  if (tx->schema_version_ != exec_ctx->schema_version_) {
    break;                                // 一个 batch 不跨 schema version
  }
  add_tx_redo_to_subtasks(*tx, *exec_ctx, added);
  exec_ctx->refresh_scn_ = max(..., tx->commit_version_);
  ++dispatch_sn_;
}
```

- `tx_ring_` 保存 transaction 顺序，不能只用一个无序 worker queue；
- schema version 变化会切断 batch，避免拿新 schema 解码旧 row；
- `refresh_scn_` 只能在 batch 内所有 subtask 都完成之后推进。

每个 subtask 中，[`ObCSAsyncIndexProcessor::process()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_cs_plugin_async_index.cpp) 先把 `ObCSRow` 转成 index event，再按 `(data_tablet_id, index_id_table_id)` 分组：

```cpp
for (const ObCSRow &row : rows) {
  resolve_table_id_from_tablet_id_(row.tablet_id_, data_table_id);
  get_or_cache_vec_index_info_(data_table_id, schema_guard, vec_infos);
  build_event_from_row_(row, vec_info, event, skip);
  groups[{row.tablet_id_, vec_info.index_id_table_id_}].push(event);
}

for (TabletEventGroup &g : groups) {
  insert_vector_index_log_batch_(g.events_, g.vec_info_); // DAS 持久元数据
  write_to_vsag_(g.events_, g.vec_info_);                 // 内存增量 HNSW
}
```

`build_event_from_row_()` 对 `INSERT` 读取 new row，对 `DELETE` 读取 old row；NULL vector 会被跳过；event 还带有 `heap_pk_` 形成的 VID、`commit_version_` 与 partition-key datum。这里还有一个值得继续验证的边界：当前函数对 `DF_UPDATE` 明确设置 `skip = true`，只处理 `DF_INSERT/DF_DELETE`。这意味着被索引 vector 的更新正确性依赖上游把相关更新编码成 delete+insert；仅凭这段静态源码，不能笼统声称任意 UPDATE 都已被异步 HNSW 正确消费。

`insert_vector_index_log_batch_()` 没有另造一条 storage API，而是现场构造 `ObDASInsertOp`、CtDef/RtDef 与 transaction context，把 index-id/delta metadata 写回 Tablet/MemTable。随后 `write_to_vsag_()` 根据 data tablet 的 partition index 找到 delta-buffer tablet，获取 adaptor 的 `incr_data_`：

```cpp
ObVectorIndexMemData *incr_data = adaptor->get_incr_data();
VectorIndexPtr hnsw = incr_data->index_;

obvectorutil::add_index(hnsw, vectors, vids, dim, nullptr, count);
adaptor->update_incr_bitmap(vids, count);
incr_data->last_dml_scn_.inc_update(last_commit_scn);
```

这里存在两种不同介质：DAS 写的是受事务保护的 durable auxiliary row，VSAG 更新的是内存图。代码用 batch transaction、epoch retry、SCN 和可重建 index metadata 协调它们，但不能把这理解为传统意义上“一个 WAL record 原子修改两个物理引擎”。内存图损坏或进程重启时仍需要依靠持久元数据恢复/重建。

这个设计是在吞吐与确定性之间折中：CPU-heavy 的解析和图更新并行，能够对外声明完成的 watermark 仍按日志顺序单调前进。某个 batch 失败时 dispatcher 增加 epoch，让受影响的后续 batch 放弃本轮结果，避免出现“后面的 SCN 已可见，前面的洞还没补上”。

Worker 的 finish path 把这个不变量写得非常直白：

```cpp
while (dispatcher.get_next_commit_sn() != ctx->batch_sn_) {
  if (ctx->epoch_ != dispatcher.get_epoch()) break;
  usleep(1000);                          // 处理并行，提交排队
}

if (task_fail) {
  ctx->trans_.end(false);                // rollback durable auxiliary writes
  dispatcher.inc_epoch();               // 后续旧 epoch batch 全部作废
} else {
  plugin->commit();
  advance_change_stream_refresh_scn(ctx->refresh_scn_);
  ctx->trans_.end(true);
  dispatcher.release_batch(ctx);         // pop tx ring，推进 next_commit_sn
}
```

`release_batch()` 只有在 commit 成功后才释放 Fetcher 中的 `ObCSTxInfo` 和 redo buffer。若 epoch 改变，Dispatcher 等所有 active batch rollback/cleanup，再把 `dispatch_sn_` 重置到 `next_commit_sn_` 重试。这是一套小型的有序 replay protocol，而不只是线程池。

### 3.3 freshness 是协议，不是形容词

主事务 commit 与向量索引可检索之间存在异步窗口。seekdb 用 `change_stream_refresh_scn` 表示 Change Stream 已完整处理到哪里；[`DBMS_INDEX_MANAGER.refresh`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/pl/sys_package/ob_dbms_index_manager.cpp) 最终调用 `ObChangeStreamMgr::wait_refresh_scn()`，等待 watermark 追上当前 safe-visible SCN。

Fetcher 推进 watermark 时采用保守证明：没有 async index table 可以直接推进到 GTS；仍有 in-flight transaction 时不推进；已经追上 Palf `max_lsn` 时推进到 GTS；否则最多推进到当前消费位置对应的 `current_scn_`。对应的伪代码是：

```cpp
if (!has_async_index)       refresh_scn = gts;
else if (!tx_info.empty())  refresh_scn = INVALID;      // 不能越过未决事务
else if (current_lsn >= max_lsn)
                            refresh_scn = gts;
else                        refresh_scn = current_scn;
```

与此同时，`change_stream_min_dep_lsn` 取所有 in-flight transaction 最早的 `start_lsn_`；没有未决事务时才等于 `current_lsn_`。一个 watermark 服务检索可见性，另一个保护日志回收，它们分别解决“读到哪里”和“日志能删到哪里”。

因此应用需要区分四种“新鲜”：

```text
transaction commit
      │ 主表数据已经提交
      ▼
Change Stream refresh_scn
      │ 已提交 redo 已被异步处理
      ▼
delta HNSW visibility
      │ 新向量进入增量图并满足 SCN/bitmap 可见性
      ▼
snapshot HNSW rebuild
        增量状态被重新组织进新的全量快照图
```

通常查询不必等待最后一步，因为它会同时搜索 delta 和 snapshot。但若业务要求严格的“刚写入就必须通过 ANN 查到”，就应该显式等待 refresh watermark，而不能把异步索引的低写延迟误解成零一致性成本。

这个源码判断已经能与产品契约交叉验证。seekdb [v1.3.0 Release Note](https://github.com/oceanbase/seekdb/releases/tag/v1.3.0)把向量索引模式明确区分为：

| `sync_mode` | 对外语义 | 代价位置 |
| --- | --- | --- |
| `immediate` | 同步刷新，提供更强的新鲜度保证 | DML 路径承担索引维护延迟 |
| `async`（默认） | 后台 Change Stream 维护，属于最终一致 | 查询前按需调用 `DBMS_INDEX_MANAGER.refresh()` 等待追平 |

这张表也修正了“查询同时搜索 delta 和 snapshot，所以刚提交数据立即可搜”的过度推断。双层 HNSW 解决的是**已进入索引体系的新旧两层如何共同检索**；Commit 到 Change Stream Watermark 之间仍有异步窗口。应用若要 Read-Your-Writes，必须选择 `immediate`，或在 `async` 模式中显式等待刷新完成，并处理 Timeout/失败。

线上可观测性也应围绕这条协议展开：`commit_scn - refresh_scn`、最老未决事务 LSN、Change Stream Queue Length、Batch Retry/Epoch、Delta HNSW 大小、Refresh Wait Time 和 Rebuild Duration，比一个笼统的“索引延迟”更容易区分日志积压、Worker 失败、图更新变慢和 Snapshot 重建压力。

## 4. 双层 HNSW：固定两层，而不是无限 segment fan-out

Change Stream 如何做到小步更新 HNSW，同时不让长期增量碎片拖垮查询？答案是两个逻辑 index instance：

- `incr_data_`：接收 Change Stream 写入的 incremental/delta HNSW；
- `snap_data_`：周期性构建的 snapshot/full HNSW。

[`ObPluginVectorIndexAdaptor`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_adaptor.cpp) 的查询代码会分别调用：

```text
knn_search(get_incr_index(), ...)
knn_search(get_snap_index(), ...)
sort_merge_delta_and_snap_vids(...)
```

最后一层 merge 对两组 candidate 按 distance 排序、去重并截断到 K；若是 iterative filtering 或某些 BQ 模式，则需要保留更多候选再做过滤。与每次 flush 产生一个新 ANN segment 的 LSM-like 设计相比，两层结构把 query fan-out 限制为常数。

### 4.1 从 SQL plan 走到 HNSW 的完整调用链

上面的三行不是一个 SQL UDF 直接调用 vector library。真正的查询路径横跨 optimizer、code generator、DAS iterator 与 adaptor：

```text
ORDER BY distance(vec, query) APPROXIMATE LIMIT K
  -> resolver 识别 vector distance / approximate / limit
  -> ObLogTableScan 持有 ObVecIndexInfo
       prepare_vector_access_exprs()
       决定 HNSW、pre-filter、post-filter 或 adaptive path
  -> ObTscCgService::generate_vec_idx_ctdef()
       生成 DAS_OP_VEC_SCAN 和多张 auxiliary-table CtDef
  -> ObDASIterUtils 创建 ObDASHNSWScanIter 及 child iter tree
  -> ObDASHNSWScanIter::process_adaptor_state_hnsw()
       acquire_adapter_guard(...tablet ids...)
       pre-filter / post-filter
  -> ObPluginVectorIndexAdaptor::query()
       incr HNSW + snap HNSW + merge
  -> VID -> rowkey -> base-table lookup
  -> SQL operator 输出最终 row
```

[`ObTscCgService::generate_vec_idx_ctdef()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/code_generator/ob_tsc_cg_service.cpp) 把一个逻辑 vector table scan 展开成一棵物理辅助表树。HNSW 分支的 child 顺序就是 storage layout 的可执行说明：

```cpp
vec_scan_ctdef->children_[0] = filter_or_index_scan;
vec_scan_ctdef->children_[1] = delta_buffer_ctdef;
vec_scan_ctdef->children_[2] = index_id_ctdef;
vec_scan_ctdef->children_[3] = snapshot_data_ctdef;
vec_scan_ctdef->children_[4] = main_table_ctdef;

// 非 VID-primary-key 优化路径还需要双向映射。
vec_scan_ctdef->children_[5] = rowkey_vid_ctdef;

// Hybrid index 再增加 embedded table；需要时还增加 functional lookup。
```

这解释了为什么“一个 HNSW index”在 schema 中会展开成多张表：optimizer 不是把它们当实现细节完全隐藏，而是 codegen 为每一张表生成 CtDef/RtDef，运行时再组装成 composite iterator。

对于 heap table + `sync_mode=async`，codegen 设置 `skip_delta_buffer_`。原因不是“不搜索增量数据”，而是这一路的增量向量已经由 Change Stream 放进 adaptor 的内存 `incr_data_`，没有必要再把传统 delta-buffer table 当作 query input 扫一遍。名字相近，但 **delta table scan** 与 **in-memory incremental HNSW search** 不是同一个动作。

[`ObDASIterUtils`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/iter/ob_das_iter_utils.cpp) 随后创建这些 child iterator，并注入 `ObDASHNSWScanIterParam`：

```cpp
hnsw_param.inv_idx_scan_iter_ = filter_iter;
hnsw_param.delta_buf_iter_     = delta_iter;
hnsw_param.index_id_iter_      = index_id_iter;
hnsw_param.snapshot_iter_      = snapshot_iter;
hnsw_param.com_aux_vec_iter_   = main_table_iter;
hnsw_param.vid_rowkey_iter_    = vid_rowkey_iter;
hnsw_param.rowkey_vid_iter_    = rowkey_vid_iter;
hnsw_param.data_filter_iter_   = iterative_filter_iter;
hnsw_param.tx_desc_            = transaction;
hnsw_param.snapshot_           = read_snapshot;
```

最后两项说明 ANN 查询也没有逃离 SQL transaction snapshot：辅助表扫描、回表与 filter 都拿到同一个 transaction descriptor/read snapshot。内存 graph candidate 只是访问路径，row visibility 仍需数据库层校验。

### 4.2 pre-filter、post-filter 与 adaptive path

[`ObDASHNSWScanIter::process_adaptor_state_hnsw()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/iter/ob_das_hnsw_scan_iter.cpp) 获取 adaptor query lock 后分成两路：

```cpp
RWLock::RLockGuard lock(adaptor->get_query_lock());

if (is_pre_filter() || is_in_filter()) {
  process_adaptor_state_pre_filter(&ctx, adaptor, vectorized);
} else {
  process_adaptor_state_post_filter(&ctx, adaptor, vectorized);
}
```

- **pre-filter**：先由 scalar/full-text/index path 得到允许的 rowkey/VID 范围，再把 filter 交给 ANN search；适合选择率高、候选集合小的条件；
- **post-filter / iterative filter**：先取 ANN candidate，再回表判断条件；若过滤掉太多结果，需要继续扩候选，直到 K 条或搜索耗尽；
- **adaptive path**：CtDef 同时携带 filter iterator、data-table iterator 和 `adaptive_try_path_`，允许运行时根据可用路径尝试更合适的策略。

这比“vector filter 被 push down”更具体：源码中同时存在 filter iterator tree、bitmap/range filter、ANN iterative context 和 base-table lookup。优化器不仅选不选 HNSW，还要决定先缩小集合还是先做近邻搜索。

### 4.3 两路结果如何合并

adaptor 分别给 `incr_data_->mem_data_rwlock_` 和 `snap_data_->mem_data_rwlock_` 加读锁，调用 VSAG `knn_search()`。查询条件中包含 `query_limit_`、`ef_search`、distance threshold、filter、range-filter 标志和 iterative-search context。

随后 [`sort_merge_delta_and_snap_vids()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_util.cpp) 做一次标准双指针 merge，但额外维护 VID hash set：

```cpp
while (result_count < limit && i < delta_count && j < snap_count) {
  candidate = delta[i].distance <= snap[j].distance ? delta[i++] : snap[j++];
  if (!seen_vid(candidate.vid)) {
    output(candidate.vid, candidate.distance, candidate.extra_info);
  }
}
```

去重不是可选优化。同一 logical row 可能在 snapshot 中有旧版本、在 incremental index 中有新版本；若只按 distance 拼接，结果会重复。删除 bitmap、SCN filter 与 VID 去重一起决定最终的逻辑可见性。

不过，“永远只有两个索引”只是查询视角的抽象。一个 HNSW logical index 在 schema/storage 中还对应多张辅助表：

- delta buffer table；
- index-id table；
- snapshot data table；
- rowkey 与 VID 双向映射；
- 可选的 hybrid embedded table；
- 更新/删除所需 bitmap 与 SCN visibility metadata。

Change Stream 的 `ObCSPluginAsyncIndex` 会把 insert vector 加到 VSAG incremental index，并更新 index-id/delta metadata；delete（以及最终被上游表达为 delete+insert 的 indexed update）不能只从 graph 中“抹掉一个点”，还要借助持久事件、bitmap 和版本可见性，使旧 VID 不再参与结果。正如前文所述，当前 plugin 本身跳过直接到达的 `DF_UPDATE`，这条转换链仍需要动态测试确认。

### 4.4 refresh 与 rebuild 不是同一件事

[`ob_vector_index_refresh.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/vector_index/ob_vector_index_refresh.cpp) 展示了两种维护动作：

- `DBMS_VECTOR.refresh_index` 处理满足条件的 delta row，把状态推进到 index-id 存储并清理旧 delta；
- rebuild 重新生成新的 full snapshot HNSW，之后查询继续保持 delta + snapshot 两路。

所以双层 HNSW 真正解决的是**流式新鲜度与查询 fan-out 的矛盾**：前台写不重建大图，新数据先进小图；查询付出固定两路搜索和合并；后台再把长期增量成本摊入 refresh/rebuild。它没有消灭维护，只是把维护从同步临界路径迁走并做了有界化。

## 5. Hybrid Search：不是客户端拼结果，而是回到 SQL Pipeline

seekdb 支持两类混合搜索入口。第一类是普通 SQL：

```sql
SELECT id, title, l2_distance(embedding, '[...]') AS dist
FROM docs
WHERE MATCH(content) AGAINST('change stream')
  AND tenant_id = 42
  AND created_at >= '2026-08-01'
ORDER BY dist APPROXIMATE
LIMIT 10;
```

vector distance、full-text predicate 和 scalar filter 进入同一 SQL plan。optimizer 可以根据 predicate、index availability 和 cost 决定访问路径，结果也不需要应用分别访问 Vector DB、Search Engine 和 OLTP 后再做 N+1 merge。

第二类是 `HYBRID_SEARCH(table, json)` 及 `DBMS_HYBRID_VECTOR` package。它看起来像独立的 JSON DSL，但源码实现很有意思：

```text
JSON search request
   -> ObESQueryParser / ObQueryReqFromJson
   -> internal request tree
   -> ObQueryTranslator
   -> generate SELECT / WHERE / ORDER BY / LIMIT / hints
   -> parser + resolver 再解析生成的 SQL
   -> ordinary optimizer / executor
```

[`ObQueryTranslator`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/hybrid_search/ob_query_translator.cpp) 不是在客户端执行 fusion，而是把 DSL 编译成 SQL，再复用数据库原有的 parser、resolver、optimizer 和 executor。这种路线的意义是只维护一个执行语义：SQL 与 JSON API 的底层 filter、排序、limit、hint 最终都落在相同引擎中。

### 5.1 为什么要“生成 SQL 再解析一次”

JSON parser 生成的 `ObQueryReqFromJson` 已经是一棵表达式树，但它不是 optimizer 的 `ObDMLStmt/ObRawExpr`。seekdb 没有再实现一套 JSON-to-plan compiler，而是让 [`ObQueryTranslator::translate()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/hybrid_search/ob_query_translator.cpp) 顺序打印 SQL 子句：

```cpp
translate_select();
translate_from();
translate_where();
translate_group_by();
translate_order_by();
if (req_->has_vec_approx()) DATA_PRINTF(" APPROXIMATE");
translate_limit();
```

其中 score item 可以被组合成 `_score`，三个以上 match index 会生成 `union_merge(...)` hint，vector request 会附加 `APPROXIMATE`。这不是简单字符串模板，而是由 typed request node 的 `translate_expr()` 负责 identifier quoting、scope 与 operator rendering。

对于 SQL table expression：

```sql
SELECT *
FROM HYBRID_SEARCH('docs', '{...}') hs;
```

[`ObDMLResolver::resolve_hybrid_search_item()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/dml/ob_dml_resolver.cpp) 的核心流程是：

```cpp
executor.execute_get_sql(hybrid_search_sql);       // JSON -> SELECT ...
parser.parse(hybrid_search_sql, parse_result);     // 第二次 parser
sub_query_wrapper->children_[0] = generated_query;
resolve_table(*sub_query_wrapper, table_item);     // 当作当前 SQL 的 subquery
query_ctx->has_hybrid_search_ = true;
```

所以 table expression 最终成为原 SQL plan 中的 subquery，不是执行期再发起一个远程检索。`DBMS_HYBRID_VECTOR` package 的 `execute_search()` 则会生成带 `hits` wrapper 的 SQL，再通过 internal `sql_proxy->read()` 执行。这两个入口复用相同 translator，但嵌入当前 plan 的时机不同。

这种设计的优点是快速继承 SQL 的权限、expression、filter、optimizer 与 transaction semantics；代价是需要严格防止 DSL 生成非法/不安全 SQL，也会经历一次额外 parser/resolver。源码中的 identifier quoting、column/index schema lookup 与 JSON typed node 正是在控制这个边界。

当前源码也暴露了功能边界：`ObHybridSearchExecutor::construct_column_index_info()` 要求 data table 使用 hidden primary key；对于用户显式提供主键的表，这条 JSON Hybrid Search 路径直接返回 `OB_NOT_SUPPORTED`。这并不代表普通 SQL 混合检索也有同样限制，但说明不能把两个入口的能力矩阵混为一谈。

## 6. FORK：从 schema clone 一直到 SSTable/macro block 的 COW

对于 Agent，sandbox 不是锦上添花。Agent 会执行概率性计划、生成代码、修改结构化状态；如果每次实验都复制一整库，成本太高，如果直接改 main state，风险又太大。seekdb 把 `FORK TABLE` / `FORK DATABASE` 做成异步 DDL：目标库得到一个一致 snapshot，之后源与 fork 都可以独立写入。

主流程如下：

```text
FORK TABLE / DATABASE
   │ clone base schema, index, LOB/vector auxiliary tables
   ▼
collect all participating tablets
   │ obtain one GTS snapshot
   ▼
DDL task
 PREPARE -> WAIT_FROZE_END -> BUILD_DATA
         -> WAIT_DATA_COMPLEMENT -> SUCCESS
   ▼
per-tablet ObTabletForkDag
   ├── SSTable 全部版本 <= fork snapshot：复用 macro blocks
   └── SSTable 跨越 fork snapshot：扫描并重写可见 row versions
   ▼
install destination tablet table store
```

[`ObForkTableUtil::obtain_snapshot()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/rootserver/fork_table/ob_fork_table_util.cpp) 会收集 base table、普通 index、domain-index auxiliary table 和 LOB tablet，使用同一个 GTS snapshot。这个细节非常重要：只 fork 主表而漏掉 vector/FTS/LOB 辅助状态，会得到逻辑上自相矛盾的 sandbox。

源码先用 GTS 生成 `new_fetched_snapshot`，再把所有 participant tablet 一次性交给 snapshot manager：

```cpp
ObDDLUtil::calc_snapshot_with_gts(new_fetched_snapshot);
snapshot_scn.convert_for_tx(new_fetched_snapshot);

for (const ObTableSchema *table : data_table_schemas) {
  collect_tablet_ids_from_table(schema_guard, *table, tablet_ids);
  max_schema_version = max(max_schema_version, table->get_schema_version());
}

snapshot_mgr.batch_acquire_snapshot(
    trans, SNAPSHOT_FOR_DDL, max_schema_version,
    snapshot_scn, nullptr, tablet_ids);
```

这里不是只“记住一个 timestamp”。`batch_acquire_snapshot()` 为所有 Tablet 持有 DDL snapshot，后续释放前，compaction/回收不能破坏该 snapshot 所需的版本。`max_schema_version` 又把数据快照与参与 schema 的上界绑定，避免 fork 过程中辅助表定义漂移。

RootService 侧 [`ObForkTableTask::process()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/rootserver/fork_table/ob_fork_table_task.cpp) 是显式状态机：

```cpp
switch (task_status_) {
  case PREPARE:              switch_status(WAIT_FROZE_END); break;
  case WAIT_FROZE_END:       wait_freeze_end(BUILD_DATA); break;
  case BUILD_DATA:           build_data(WAIT_DATA_COMPLEMENT); break;
  case WAIT_DATA_COMPLEMENT: wait_data_complement(SUCCESS); break;
  case SUCCESS:              succ(); break; // cleanup + release snapshot
  case FAIL:                 fail(); break;
}
```

因此一次 SQL 返回之前并不是在当前 worker 内循环复制所有行。RootService 建立可恢复的 DDL task，按 tick 推进状态；`BUILD_DATA` 再为各 Tablet 调度 DAG。`WAIT_FROZE_END` 的意义是让 fork snapshot 对应的数据边界可由稳定 table store 表达，而 `WAIT_DATA_COMPLEMENT` 汇总各 Tablet 的完成结果。

进入 storage 层后，[`ObTabletForkDag`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/ddl/ob_tablet_fork_task.cpp) 按 SSTable 判断能否复用：

- 若 SSTable 中所有版本在 fork snapshot 之前，`process_reuse_sstable()` 为目标 tablet 创建指向既有 macro block 的 SSTable metadata；
- 若 SSTable 同时包含 snapshot 前后的版本，就不能整块共享，`ObForkSnapshotRowScan` 过滤掉 snapshot 之后的 row version，再生成目标 SSTable。

DAG 的构造代码直接表达了这个分支：

```cpp
for (ObSSTable *sstable : source_sstables) {
  bool fully_visible = sstable->is_major_sstable()
      ? sstable->get_end_scn() <= fork_snapshot
      : sstable->get_upper_trans_version() <= fork_snapshot;

  if (fully_visible) {
    prepare_task -> reuse_task(sstable) -> merge_task;
  } else {
    prepare_task -> rewrite_task(sstable) -> merge_task;
  }
}
```

`reuse_task` 调用 `build_migration_sstable_param(..., is_fork_table=true)`，再用 `ObTabletCreateSSTableParam::init_for_fork()` 替换 destination tablet ID，创建共享既有 block 的目标 SSTable。后续 fork 与 source 的新写入分别进入各自 Tablet 的新 MemTable/SSTable，因此共享的是不可变历史 block，不是两个表共同修改一块可变 page。

`rewrite_task` 则实例化 `ObForkSnapshotRowScan`。它构造的 read context 明确使用 fork SCN：

```cpp
snapshot_scn.convert_for_tx(fork_snapshot_version);
ctx_.init_for_read(ls_id, tablet_id, ..., snapshot_scn);
version_range.snapshot_version_ = fork_snapshot_version;
```

扫描到 multi-version row 后还会防御性检查 transaction version：

```cpp
if (abs(trans_version) > fork_snapshot_version_) {
  continue;                         // snapshot 之后的版本不能进入 sandbox
}
macro_block_writer->append_row(row); // 只重写 fork 时可见的数据
```

最后 `ObTabletForkMergeTask` 汇总 reuse/rewrite 产生的 SSTable handle，安装 destination tablet table store。这里的 “merge” 是物理构建结果合并，与后面的 SQL `MERGE TABLE` 完全不是同一个机制。

所以它确实是 storage-level COW，而非应用层 `SELECT INTO`；但也不能简单说成“fork 永远是瞬时 metadata-only”。跨 snapshot 边界的 SSTable 需要重写，整个操作还有 freeze、per-tablet DAG 和 data complement 阶段。源码测试覆盖了源表/fork 表并发写、snapshot isolation、LOB、partition、vector/full-text index 和 COW 隔离，这些比单个 demo 更能说明实现范围。

## 7. MERGE TABLE 的真实边界：按主键对账，不是 Git 三方合并

README 使用 Git/Sandbox 类比很自然，但阅读 [`ObMergeTableResolver`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/cmd/ob_merge_table_resolver.cpp) 后需要加一个关键限定：当前 `MERGE TABLE incoming INTO current` 没有读取 fork 时的共同祖先，也没有对 source、sandbox、base 做 three-way diff。

resolver 校验两张表的 primary key/value columns，然后生成普通 SQL；executor 通过 inner SQL connection 在一个 transaction 中执行。三种策略实际语义是：

| Strategy | incoming 独有 PK | 相同 PK、value 不同 |
| --- | --- | --- |
| `FAIL` | 插入 | `COUNT(*) ... FOR UPDATE` 检测到后报错 |
| `THEIRS` | 插入 | 用 incoming value 更新 current |
| `OURS` | 插入 | 保留 current value |

源码生成的 SQL 骨架更容易看出语义：

```sql
-- 三种策略都会执行：只复制 incoming 独有主键
INSERT INTO current(pk, values...)
SELECT i.pk, i.values...
FROM incoming i LEFT JOIN current c ON i.pk = c.pk
WHERE c.pk IS NULL;

-- 只有 THEIRS：同 PK 且 value 不同时覆盖
UPDATE current c JOIN incoming i ON c.pk = i.pk
SET c.v1 = i.v1, c.v2 = i.v2, ...
WHERE NOT (c.v1 <=> i.v1 AND c.v2 <=> i.v2 ...);

-- FAIL：执行 INSERT 前先锁住并计算冲突
SELECT COUNT(*) AS conflict_cnt
FROM current c JOIN incoming i ON c.pk = i.pk
WHERE NOT (... null-safe value equality ...)
FOR UPDATE;
```

[`ObMergeTableExecutor::execute()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/cmd/ob_merge_table_executor.cpp) 若当前 session 尚未开启事务，就由 inner connection `start_transaction()`，依次执行 conflict check、insert、update，最后整体 commit/rollback；若调用者已有显式事务，则加入已有事务边界。

还要注意一个常被 “merge” 一词掩盖的行为：**current 中存在、incoming 中不存在的主键不会被删除**。也就是说，sandbox 的 DELETE 不能仅凭最终表内容传播回 main；系统没有 tombstone/change set 来区分“sandbox 主动删除”与“fork 后 main 新增”。

这是一种实用的 **primary-key reconciliation**，而不是版本控制系统中的语义 merge。考虑一行在 fork 后只被 main 修改、sandbox 从未碰过：`THEIRS` 仍可能用 sandbox 中的旧值覆盖 main，因为系统没有共同祖先来判断“哪一边真正改过”。反过来，删除、字段级冲突与业务语义也不能仅靠三种 row-level strategy 自动推断。

因此更准确的使用方式是：把 `FORK` 看成物理存储的可写 snapshot，把 `MERGE TABLE` 看成显式策略驱动的表级导入/对账。若要达到真正 “Git for data”，还需要持久化 lineage/base version、三方 change set、delete/tombstone 语义、schema evolution merge 和可审计的 conflict object。

## 8. Embedded：没有网络 hop，不等于没有 Server 内核

[`seekdb.h`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/embed/c/seekdb.h) 暴露了 C API：`seekdb_open/connect/execute`，以及 SQLite 风格的 `prepare/step/bind/finalize`。Python 和 Android binding 建立在相同实现之上。

`ObLiteEmbed::do_open_()` 的行为说明了 “embedded” 的确切含义：

1. 设置 `embed_mode_`，创建 `store/redo`、`slog`、`sstable`、`run` 和 `log` 目录；
2. 仍然调用 `OBSERVER.init()` 与 `OBSERVER.start()`，等待 root service full service；
3. `connect()` 创建 `ObSQLSessionInfo`，从 inner SQL connection pool 获取 `ObInnerSQLConnection`；
4. `execute()` 按读写调用 `execute_read()` / `execute_write()`，prepared statement 也进入同一 SQL/事务栈。

启动代码去掉目录和日志配置后可整理成这条等价主线：

```cpp
ObServerOptions opts;
opts.embed_mode_ = true;
opts.parameters_.push_back({"memory_limit", "1G"});
opts.parameters_.push_back({"log_disk_size", "2G"});

OBSERVER.init(opts, log_cfg);
OBSERVER.start(opts.embed_mode_);
while (!GCTX.root_service_->is_full_service()) {
  ob_usleep(100 * 1000);
}
```

也就是说，`open()` 不是 mmap 一个文件然后立即返回。它创建 redo/slog/sstable 目录，初始化 timestamp、I/O、multi-tenant、storage object、log pool、RootService 与 OceanBase service，并等待完整服务状态。Embedded 的首开延迟和基线资源因此来自真实数据库启动。

连接也不是一个无状态函数对象：

```cpp
session_mgr_->create_session(1UL, sid, ObTimeUtility::current_time(), session);
session->load_all_sys_vars(schema_guard);
session->set_default_database(db_name);
session->set_autocommit(autocommit);
session->set_real_client_ip_and_port("127.0.0.1", 0);
OBSERVER.get_inner_sql_conn_pool().acquire(session, inner_conn);
```

每个 embedded connection 都有 `ObSQLSessionInfo` 和 `ObInnerSQLConnection`，因此 session variable、显式 transaction、prepared statement 和 warning buffer 仍按 SQL session 管理。C API 的 `seekdb_execute()` 执行后还会检查 `need_autocommit()` 并调用 `commit()`，不是每条写入直接绕过事务。

当前 `ObLiteEmbedConn::execute()` 通过 SQL 前缀区分读写：`SELECT/SHOW/DESC/DESCRIBE/EXPLAIN/WITH` 走 `execute_read()`，其他语句走 `execute_write()`。prepared statement 路径则调用真正的 `stmt_prepare/stmt_execute`。这个前缀分流是 wrapper 的工程实现，不是核心 parser 的 statement classification；遇到新的 statement 形态时需要同步维护。

所以 Embedded 消除的是部署独立进程和 MySQL protocol network hop，保留的是完整 observer/SQL/storage engine。这让本地与 server 模式语义更一致，却也意味着启动、内存、日志目录和生命周期仍有数据库内核的重量。

当前提交还有一个值得注意的工程边界：`ObLiteEmbed::close()` 会 `_Exit(0)`；C wrapper 的 `seekdb_close()` 为避免杀死宿主进程，明确**不调用**它，只释放 API handle，并留下 “graceful embedded shutdown” 待后续实现。另一个限制是 `GCTX.is_inited()` 防止重复 open，数据目录还有 pid lock，因此不能把它理解为在同一进程随意创建多个完全隔离的轻量 instance。

## 9. AI Service：模型治理进入数据库，推理仍可在外部

seekdb 的 AI-Native 也不意味着数据库内核自己训练或托管所有模型。[`EndpointType`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/share/ai_service/ob_ai_model_info.h) 定义了 `DENSE_EMBEDDING`、`SPARSE_EMBEDDING`、`COMPLETION` 和 `RERANK`；`DBMS_AI_SERVICE` 管理 endpoint，元数据包括 URL、access key、provider、request model name、parameters，以及可选 request/response transform function。

以 `AI_EMBED(model_id, content[, dimension])` 为例：

```text
SQL expression evaluation
   -> tenant AI service guard
   -> lookup endpoint by ai_model_name
   -> ObAIFuncModel::call_dense_embedding
   -> HTTP model endpoint
   -> parse float/base64 embedding
   -> return VECTOR result / feed index build
```

SQL expression 的核心执行代码可以压缩成：

```cpp
ObTenantAiService *svc = share::g_mp->tenant_ai_service();
svc->get_ai_service_guard(guard);
guard.get_ai_endpoint_by_ai_model_name(model_id, endpoint);

ObAIFuncModel model(allocator, parsed_func_info, *endpoint);
model.call_dense_embedding(content, config, result);
```

这里先通过 tenant-local service guard 查 endpoint，而不是每行直接查系统表。Endpoint 的创建/修改/删除由 `DBMS_AI_SERVICE -> ObAiServiceExecutor -> ObAiServiceProxy` 完成，proxy 把 `endpoint_name/model_name/url/access_key/provider/parameters/transform_fn` 写入内部 catalog；tenant service 再对执行侧提供一致的 endpoint view。

Vector index 的 bulk embedding 还有 [`ObEmbeddingTask`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_vector_embedding_handler.cpp)：它用 cURL multi 发送 batch request，维护 `INIT -> HTTP_SENT -> HTTP_COMPLETED -> PARSED -> DONE` 状态机，实现 timeout、指数退避 retry、batch 调整、float/base64 response decode 和 callback 写回。

这两条调用路径需要分开理解：

- `AI_EMBED()` 出现在普通 SQL expression 中时，模型 latency 直接进入该 query 的 critical path；
- vector index build/refresh 使用 `ObEmbeddingTask` 时，可以分 batch、异步 poll cURL multi，并通过 callback 把向量交给后续 storage/index build。

`ObEmbeddingTask` 的状态转换受到 `ObEmbeddingTaskPhaseManager` 检查；HTTP 只在 200 response 后进入 parse，float 数组与 base64 embedding 分别解码，并检查 dimension。网络错误采用有上限的 retry/backoff，而不是无界重试阻塞 DDL。

把 endpoint 与 SQL function 放进数据库的价值在于统一 credential、model alias、batching、retry 和数据路径，应用不必先全表拉出文本再逐行发 HTTP。但故障域也被扩展了：外部模型 latency、rate limit、credential 和 response schema 会进入 query/index build 路径。生产系统仍需明确哪些调用是同步 query expression，哪些是异步 index pipeline，并对 endpoint 权限和超时做治理。

### 9.1 推荐的源码阅读顺序

seekdb 源码体量很大，从目录第一页顺序读效率很低。若目标是系统理解本文主题，我建议按“主状态 → 派生状态 → 查询 → 分支”的因果顺序：

| 阶段 | 从哪里开始 | 读到哪里为止 | 要回答的问题 |
| --- | --- | --- | --- |
| 1. 启动与接入 | `ob_server.cpp`、`ob_embed_impl.cpp` | `obmp_query.cpp`、inner SQL connection | Embedded 与 Server 共用了什么？ |
| 2. SQL 主写路径 | `ob_sql.cpp` | `ob_das_insert_op.cpp`、`ob_access_service.cpp` | SQL 如何定位 LS/Tablet？ |
| 3. MVCC 与 redo | `ob_tablet.cpp` | `ob_memtable.cpp`、`ob_mvcc_trans_ctx.cpp` | old/new row 如何进入 callback 与 redo？ |
| 4. Change Stream | `ob_change_stream_fetcher.cpp` | dispatcher、worker、async-index plugin | commit order、schema version、失败重试如何保证？ |
| 5. Vector 查询 | `ob_log_table_scan.cpp` | `ob_tsc_cg_service.cpp`、`ob_das_hnsw_scan_iter.cpp` | 一个 logical scan 如何展开成辅助表树？ |
| 6. HNSW 内存层 | `ob_plugin_vector_index_adaptor.cpp` | `ob_plugin_vector_index_util.cpp` | 增量/快照、bitmap、VID merge 如何组合？ |
| 7. Hybrid DSL | `ob_query_parse.cpp` | translator、DML resolver | JSON 如何重新进入 SQL optimizer？ |
| 8. Sandbox | `ob_fork_table_util.cpp` | `ob_tablet_fork_task.cpp`、merge-table resolver | physical COW 与 logical reconciliation 差在哪里？ |

每一阶段最好画出四样东西：入口对象、持久状态、并发/事务边界、失败后从哪里重试。只记类名很快会迷失；持续追踪 `tx_desc`、`schema_version`、`SCN/LSN`、`tablet_id` 这四类 identity，调用链会稳定很多。

### 9.2 七个必须同时成立的系统不变量

把前面的源码收束起来，seekdb 的实时向量能力依赖至少七条不变量：

1. **主表先成为事务事实**：未 commit 的 row redo 不能进入可声明完成的向量 watermark；
2. **rollback 可重放**：abort 与 rollback-to-savepoint 必须在派生 event 前被过滤；
3. **schema 不穿越 batch**：同一 batch 只能按一个 schema version 解码 old/new row；
4. **处理可并行，完成点有序**：worker 可并行，`refresh_scn` 必须按 transaction ring 顺序推进；
5. **日志不能早删**：`min_dep_lsn` 必须覆盖最早 in-flight transaction；
6. **查询必须合并新旧世界**：snapshot 与 incremental candidate 要经过 bitmap/SCN/VID 去重；
7. **回表共享 read snapshot**：ANN candidate、auxiliary mapping 与 base row 不能各自使用不同可见性时刻。

单看任何一个模块都无法证明端到端正确性。例如 HNSW 返回正确近邻，不代表它包含所有已承诺可见的 transaction；Change Stream 顺序正确，也不代表 query 没有把已删除 snapshot VID 重新带回。AI-Native 特性真正的工程难点正是跨模块不变量。

## 10. 与 BigQuery、Snowflake、Databricks 的方向对照

工业界正在明显收敛到“结构化数据 + 向量 + 模型调用 + 治理”，差异主要在这些能力落在哪一层。

| 方向 | 代表能力 | 与 seekdb 的关系 |
| --- | --- | --- |
| Warehouse 增加 Vector/AI SQL | [BigQuery Vector Search](https://cloud.google.com/bigquery/docs/vector-search-intro) 与 AI functions | 都希望 vector 与 SQL/filter 共存；BigQuery 更接近云数仓内的托管分析与索引服务 |
| Search 作为托管服务 | [Snowflake Cortex Search](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview) | 强调从表数据构建 hybrid search service；控制面和 serving endpoint 更外显 |
| Lakehouse 增加在线向量索引 | [Databricks Mosaic AI Vector Search](https://docs.databricks.com/en/generative-ai/vector-search.html) | 可由 Delta table 同步索引；数据/索引/在线 endpoint 仍是清晰分层的 managed resource |
| 单机事务内核内置检索与沙箱 | seekdb | Change Stream、HNSW、FTS、SQL、tablet COW 与 transaction/log 在同一代码库和一致性域 |

这里很难用“谁更 AI-Native”做单轴排名。Cloud warehouse 擅长弹性算力、湖仓数据治理、模型生态和 managed endpoint，适合大规模预处理、特征生成与离线评估；seekdb 当前源码更强调低延迟持续写、嵌入式/单机形态和事务状态 sandbox。它们是在不同系统边界上解决相似的融合问题。

对于“预训练数据清洗”，seekdb 具备 SQL、全文、向量和模型函数，可以参与去重、检索与标注，但它并未因此取代 BigQuery/Snowflake/Databricks 这类分布式 scan engine。大规模 columnar scan、shuffle、弹性计算与训练数据 pipeline 仍是 OLAP/lakehouse 的主场。AI-Native database 不应成为忽略 workload shape 的万能标签。

## 11. 与学术路线的联系：新鲜度是 Vector Index 的核心矛盾

seekdb 的两层方案可以放到 ANN 研究脉络中理解：

- [HNSW](https://arxiv.org/abs/1603.09320) 用分层 small-world graph 获得高 recall/低延迟，但在线 mutation 的并发与长期图质量并不是免费的；
- [DiskANN](https://proceedings.neurips.cc/paper/2019/hash/09853c7fb1d3f8ee67a61b6bf4a7f8e6-Abstract.html) 关注十亿规模、SSD 上的高效图检索；
- [FreshDiskANN](https://arxiv.org/abs/2105.09613) 直接研究 streaming insert/delete 下如何维持 recall 与性能；
- [SPFresh](https://doi.org/10.1145/3600006.3613166) 则探索 billion-scale 向量索引的 in-place incremental update。

这些工作共同说明，Vector DB 的难题早已不只是“实现一个 KNN 算法”，而是 update visibility、delete、rebuild、concurrency、memory/SSD layout 与 tail latency。seekdb 没有照搬上述某一篇论文，而是选择 `redo -> delta HNSW -> snapshot HNSW`：用数据库日志确定已提交变化，用两层查询保证新旧数据共同可搜，再用后台 rebuild 控制长期结构。

FORK 的研究背景则更接近 database snapshot、MVCC、zero-copy clone 和 storage COW，而不是 ANN。它把 Agent sandbox 的产品概念下沉到 tablet/SSTable/macro block；这个跨层组合，可能比再发明一种 distance function 更符合 “AI state database” 的本质。

## 12. 如果今天从头设计，还缺什么

源码给我的启发不是“所有 AI database 都应该复制 seekdb”，而是可以列出一份更严格的 blank-slate checklist：

### 12.1 把 freshness 写进 API

查询应该能声明 `eventual`、`read-your-writes`、`as-of watermark` 等检索一致性，而不是让用户猜 refresh 是否完成。watermark、index lag、fallback exact scan 和 cost 应成为 optimizer 可见的 property。

### 12.2 把 retrieval 当作一等算子

Vector ANN、BM25/full-text、graph traversal、scalar filter、rerank 和 model call 应能组成一个 costed plan；optimizer 需要理解 candidate cardinality、recall budget、filter selectivity、模型 latency 与 token cost，而不仅是把 UDF 当黑盒。

### 12.3 原生 lineage-aware branch

真正的数据 branch 应保存共同祖先和 change set，支持 row/field/schema 三层冲突、delete semantics、branch quota、TTL、审计和可重复 merge。seekdb 的物理 FORK 已经走到 storage 层，但当前 MERGE 仍需要更丰富的版本语义。

### 12.4 多模态不只是一个 VECTOR column

图片、音频、文档 chunk、结构化 entity 与它们的多个 embedding version 需要 lineage：由哪个模型、哪个 prompt、哪次解析产生；模型升级后哪些向量过期；原始对象与派生索引如何原子切换。

### 12.5 Agent 安全进入事务与资源模型

Agent 的 query budget、tool permission、sandbox capability、模型 credential、PII policy 和 prompt-injection provenance 都应该可审计。数据库不能只保证 ACID，还要限制一个自主循环能扫描多少数据、调用多少次模型、保留多久的临时分支。

## 13. 最后的判断

回到开头的问题：真正 AI-Native 的 engine 长什么样？从 seekdb 当前源码，我得到的答案不是一个全新的 page format，而是一组围绕 Agent state 生命周期的跨层协议：

```text
continuous transactional writes
          │ redo
          ▼
Change Stream + freshness watermark
          │
          ▼
delta HNSW + snapshot HNSW
          │
          ├── full-text + scalar filters in SQL plan
          ├── managed embedding/rerank/completion endpoints
          └── tablet/SSTable COW sandbox
```

seekdb 最有价值的地方，不是证明传统 database kernel 已经过时，反而是证明 AI workload 仍然需要它：transaction、redo、snapshot、schema、SQL optimizer、SSTable 和 compaction，为 vector freshness 与 Agent sandbox 提供了可靠地基。

它当前的边界也很清楚：异步索引存在可见性窗口；双层查询要付出 merge/bitmap/rebuild 成本；JSON Hybrid Search 对表形态有限制；FORK 某些 SSTable 需要重写；MERGE 不是三方版本合并；Embedded 的 graceful shutdown 与多实例隔离仍不完整；模型 inference 依赖外部 endpoint。

因此我会把 seekdb 定义为：**一款基于成熟 OceanBase 数据库内核、针对 AI Agent 状态与实时混合检索做垂直整合的引擎**。它不是 blank-slate AI kernel，但它提出的问题——新状态何时可搜、结构化与语义检索如何共存、Agent 如何安全试错——比“支持 VECTOR 类型”更接近 AI-Native 的真正含义。

## 14. 阅读与验证边界

本文结论来自 `1e9b720c` 源码静态阅读，重点交叉检查了 Change Stream、vector index adaptor/refresh、Hybrid Search translator、FORK/MERGE、Embedded 与 AI Service，并参考仓库中的 fork-table mysql tests。本文没有完成 seekdb 全量编译、性能 benchmark 或故障注入测试；涉及吞吐、P99 和具体产品版本的数字应以对应 benchmark 环境与官方文档为准，而不由本文的源码阅读直接证明。

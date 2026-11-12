---
title: "【源码】seekdb：AI-Native 数据库的 Change Stream、双层 HNSW 与 COW Sandbox"
slug: "seekdb-ai-native-source-notes"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从 Postgres 拼外挂向量库在双写漂移、过期幽灵召回与 Agent 分支拷贝上撞碎的三道工程墙出发，深入 seekdb 源码拆解事务日志驱动的 Change Stream、增量与快照双层 HNSW，以及宏块级 COW FORK/MERGE 的真实边界。"
draft: false
toc: true
math: false
---

构建生产级 AI Agent 系统时，如果用最朴素、最省事的做法管状态——左边摆一套 PostgreSQL 存结构化会话、租户权限与业务表，右边挂一套独立的 Milvus 或 Qdrant 存文档与记忆切片的 Embedding 向量，中间靠几百行 Python 胶水代码双写同步——第一步会在哪里撞墙？

只要让一个多步自主规划的 Agent 循环真正跑起来，这套“关系库 + 外挂向量库”的拼接架构很快就会在三个地方同时漏水：

1. **跨系统双写漂移与回滚幽灵（Dual-Write Drift & Stale Deletes）**：Agent 在一笔工具调用事务里更新了记忆表、扣减了配额，正准备发 HTTP 请求更新外挂向量库时进程崩了；或者工具执行报错触发了 SQL `ROLLBACK TO SAVEPOINT`。关系库干脆利落地回退了，外挂向量库里却永远留下了一条脏向量，或者主表早已删除的记录仍在 Top-K 近邻里被频频召回。反过来，如果你为了强一致硬把 HNSW 图更新塞进在线 SQL 事务的同步提交路径里，每插入一行就要在多层小世界图（Small-World Graph）上跨几十个邻居节点加锁连边，在线写入的 P99 延迟当场就会炸穿。
2. **标量/全文/向量的分裂过滤与段扇出失控**：真实 Agent 的一次检索几乎永远带着复合条件——“查 `tenant_id = 42`、最近一小时内写入、正文包含关键词 `'change stream'` 且向量语义最相近的前 10 条记忆”。拆在两套系统里，要么先从向量库盲捞 1,000 条回关系库筛掉 999 条导致召回率断崖下跌，要么把所有业务字段全量冗余同步进向量引擎。而在流式高频写入下，如果向量引擎每分钟刷盘生成一个独立的不可变 ANN 段（Segment），一次查询就得同时探测几十张小图，读放大迅速失控。
3. **Agent 试错分支的全量拷贝高墙**：编程或运维 Agent 经常需要做推测执行（Speculative Execution）——“先切出一条状态分支大胆修改配置表和知识库，跑一遍验证测试；过了就合回主线，挂了就直接扔掉”。在传统架构上，每跑一次 30 秒的推理实验难道要把几十 GB 的关系表连同 HNSW 向量索引全量物理拷贝一份？

带着这三道真问题去读 OceanBase 开源的 [seekdb](https://github.com/oceanbase/seekdb)（定位为 *The State Store for AI Agents*），你会立刻看懂它真正的架构底牌：**seekdb 没有从零另起炉灶写一个孤立的纯向量引擎，而是直接站在 OceanBase 久经考验的 SQL、事务、Palf Redo 日志、Tablet、MemTable/SSTable 与 Compaction 内核之上，用同一条事务日志流串起了实时双层 HNSW，并用底层存储宏块的写时复制（Copy-on-Write, COW）支撑起了毫秒级状态分支。**

褪去宣传胶片的光环，真正吸引存储工程师的是它在代码深处划出的硬边界：异步 Change Stream 如何精确过滤未提交与局部回滚的日志？“双层 HNSW”在优化器与存储层怎样展开成多张辅助表与删除位图？`FORK TABLE` 在什么条件下零拷贝复用宏块、什么条件下仍必须过滤重写？而 `MERGE TABLE` 为什么只是主键级二方对账，而非 Git 式的三方语义合并？下面我们直接进源码一探究竟。

---

## 一、把状态生命周期收归同一个事务域：底座分工与主写闭环

单次大模型推理本身是无状态的；让 Agent 具备跨轮次执行能力的，是底下四类读写特征迥异的持久状态：

| Agent 状态类别 | 典型存储内容 | 对底层数据库内核的硬约束 |
| --- | --- | --- |
| **工作状态（Working State）** | 当前任务栈、工具调用上下文、计划检查点 | 微秒/毫秒级事务读写、严格原子回滚与崩溃恢复 |
| **情景记忆（Episodic Memory）** | 历史对话流、工具返回事件、执行轨迹 | 高频持续追加写入、时间范围过滤与全文关键词检索 |
| **语义记忆（Semantic Memory）** | 文档 Chunk、实体知识、多维 Embedding 向量 | 近似最近邻（ANN）检索、租户/权限标量过滤、流式新鲜度 |
| **程序性状态（Procedural State）** | Prompt 版本、工作流策略、实验沙箱配置 | 结构化关联查询、低成本隔离分支（Fork）与审计回滚 |

要把这四类状态装进同一个进程、同一个一致性域，seekdb 在内核里划清了三层绝不混用的状态角色：

- **第一性事实源（Primary State）**：用户主表的 MVCC 行版本、MemTable/SSTable 以及 Palf 持久化 Redo 日志，它是全系统唯一的真相裁决者；
- **可重建派生状态（Derived State）**：内存增量 HNSW、全量快照 HNSW、全文索引（FTS）以及各类双向映射与位图辅助表，它们全部从已提交的事实源派生，但读取时必须服从数据库快照可见性；
- **水位与控制状态（Control State）**：Schema 版本、`change_stream_refresh_scn` 新鲜度水位、DDL 快照租约与 Fork DAG 任务状态机，负责卡住上述两者的推进与垃圾回收边界。

阅读源码前，可以先按下面这张索引表定位核心模块：

| 核心机制 | 对应源码目录与核心实现文件 |
| --- | --- |
| 事务日志驱动 Change Stream | [`src/observer/change_stream/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/change_stream) |
| 双层 HNSW 检索与双指针归并 | [`ob_plugin_vector_index_adaptor.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_adaptor.cpp)、[`ob_plugin_vector_index_util.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_util.cpp) |
| 向量索引后台 Refresh 与 Rebuild | [`src/storage/vector_index/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/storage/vector_index) |
| 混合搜索 JSON DSL 转译器 | [`src/sql/hybrid_search/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/sql/hybrid_search) |
| 存储级 COW `FORK TABLE` | [`src/rootserver/fork_table/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/rootserver/fork_table)、[`ob_tablet_fork_task.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/ddl/ob_tablet_fork_task.cpp) |
| 主键对账 `MERGE TABLE` | [`ob_merge_table_resolver.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/cmd/ob_merge_table_resolver.cpp)、[`ob_merge_table_executor.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/cmd/ob_merge_table_executor.cpp) |
| 嵌入式 C/Python API 与模型服务 | [`src/observer/embed/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/embed)、[`src/observer/ai_service/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/ai_service)、[`ob_expr_ai_embed.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/expr/ob_expr_ai/ob_expr_ai_embed.cpp) |

我们先跟踪一条最普通的带向量列 `INSERT` 语句如何落进主存储。无论请求来自服务端 MySQL 协议入口 [`ObMPQuery::do_process()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/mysql/obmp_query.cpp) 还是嵌入式进程内连接 `ObLiteEmbedConn::execute()`，都会汇入 [`ObSql::handle_text_query()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/ob_sql.cpp)，经解析、优化与代码生成后，由数据访问算子 [`ObDASInsertOp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/ob_das_insert_op.cpp) 穿过 [`ObAccessService::insert_rows()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/tx_storage/ob_access_service.cpp) 与 [`ObTablet::insert_rows()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/tablet/ob_tablet.cpp)，最终写入活跃内存表 [`ObMemtable::set_()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/memtable/ob_memtable.cpp)：

```cpp
build_row_data_(..., old_row_data, new_memtable_data); // 序列化变更前后的行镜像
mvcc_write_(ctx, memtable_key, tx_node, ...);          // 挂入未提交的 MVCC 内存节点
check_row_locked_on_frozen_stores_(...);               // 核查冻结层写写冲突与唯一键约束

mem_ctx->register_row_commit_cb(...);                  // 注册事务提交/回滚与 Redo 生成回调
mvcc_engine_.finish_kv(mvcc_result);                   // 完成内存索引节点链接
```

奥秘就在倒数第二行的 `register_row_commit_cb()` 里：前台 DML 既没有去直接改内存里的 HNSW 图，也没有单独往消息队列里双写一份旁路事件；它只是按标准事务流程注册了一个 `ObMvccRowCallback`。等到事务提交前夕，`ObMvccRowCallback::get_redo()` 把主键、新旧行字节、DML 类型、语句序号与 `tablet_id` 打包写进 Palf Redo 日志。从此，**Palf 里的已提交事务日志就成了下游向量索引演进的唯一事实输入**，应用层双写漂移的根源被连根拔除。

---

## 二、Change Stream 与双层 HNSW：把流式新鲜度、位图过滤与常数扇出拧成闭环

主表秒级提交了，那向量什么时候能被 ANN 搜到？删除或回滚的旧向量怎么屏蔽？seekdb 给出的解法是一条从 Palf 日志直通增量/快照双层 HNSW 的流水线：

![seekdb 统一事务日志驱动的双层 HNSW 与混合检索数据流](/images/diagrams/aug-store-seekdb-changestream-hnsw.svg)

### 2.1 `ObCSFetcher` 与有序批次重放：绝不把“裸尾巴日志”当成已提交事实

如果让你写一个异步消费 WAL 更新向量索引的后台线程，最容易犯的致命错误就是“从日志末尾读到一条 Insert Redo 就立刻往 HNSW 图里塞一个点”——因为大事务在提交前早就分段刷出了多条 Redo，中途还可能执行 `ROLLBACK TO SAVEPOINT` 撤回其中几条语句，最后甚至可能整笔 `ABORT`！

在 [`ObCSFetcher`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_fetcher.cpp) 中，拉取线程用一张按事务号聚合的暂存表 `ObCSTxInfo` 把这四类日志事件严格还原成事务级可见结果：

```cpp
struct ObCSTxInfo {
  int64_t tx_id_;                         // 事务聚合主键
  int64_t commit_version_;                // 仅在收到 Commit 日志时才赋正式提交 SCN
  palf::LSN start_lsn_;                   // 卡住底层日志回收的最老下界
  int64_t schema_version_;                // 绑定解码行字节所需的 Schema 版本
  ObArray<ObCSRollbackRange> rollback_list_;
  ObArray<ObCSRedoRecord> redo_list_;
};
```

只有当 `handle_commit_log_` 真正抵达时，该事务才会带着确定的 `commit_version_` 被推入下游环形队列（若收到 `handle_abort_log_` 则整笔原地抹除；若曾收到 `handle_rollback_to_log_`，则后续解码时通过 `rollback_list_` 精准剔除回滚区间内的语句）。与此同时，Fetcher 持续取所有在途未决事务中最早的 `start_lsn_` 更新为 `change_stream_min_dep_lsn`——底层日志归档回收必须停在这个水位之前，宁可多占一点日志盘，也绝不把异步索引还没消费完的 Redo 删掉（这也揭示了长事务在 AI-Native 库里的新风险：一笔迟迟不提交的大事务不仅撑大 Fetcher 内存暂存，还会直接卡住 Palf 日志回收）。

紧接着，[`ObCSDispatcher`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_dispatcher.cpp) 与 [`ObCSWorker`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_change_stream_worker.cpp) 执行了一套经典的**“并行建图、串行推进水位”**协议：

1. Dispatcher 从有序环 `tx_ring_` 取已提交事务攒批，一旦检测到相邻事务的 `schema_version_` 发生变化就立即切分批次，确保同一个 Batch 内永远用同一版 Schema 解码新旧行；
2. Batch 内部按主键切分成多个并行 Subtask，交由 [`ObCSAsyncIndexProcessor::process()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/change_stream/ob_cs_plugin_async_index.cpp) 多核并发运行：一边构造内部 `ObDASInsertOp` 将持久化的索引元数据（index-id / delta 日志表）写回存储层，一边调用 VSAG 向量库接口 `obvectorutil::add_index()` 把新向量插进内存增量图 `incr_data_`，并同步更新删除/可见位图（注：静态阅读该处的 `build_event_from_row_()` 可以看到，代码显式处理了 `DF_INSERT` 与 `DF_DELETE`，而对直接传入的 `DF_UPDATE` 标记置 `skip = true`，这意味着被索引向量列的更新依赖上游将其拆解表达为 `DELETE + INSERT` 事件对）；
3. 到了提交阶段，尽管多个 Batch 可以并行算完，每个 Worker 却必须按 `batch_sn_` 排队等待 `dispatcher.get_next_commit_sn()` 轮到自己，才能按严格日志顺序推进全局可见水位 `change_stream_refresh_scn`：

```cpp
while (dispatcher.get_next_commit_sn() != ctx->batch_sn_) {
  if (ctx->epoch_ != dispatcher.get_epoch()) break;
  usleep(1000);                          // 建图可乱序并行，水位提交严格串行排队
}

if (task_fail) {
  ctx->trans_.end(false);                // 回滚已写入的辅助表元数据事务
  dispatcher.inc_epoch();                // 自增 Epoch 作废后续全部在途批次，退回有序起点重试
} else {
  plugin->commit();
  advance_change_stream_refresh_scn(ctx->refresh_scn_);
  ctx->trans_.end(true);
  dispatcher.release_batch(ctx);         // 释放 Fetcher 暂存并唤醒下一号批次
}
```

这套有序水位的最大价值，就是把模糊的“最终一致”变成了应用可编程的**新鲜度契约（Freshness Contract）**。对照 seekdb [v1.3.0 Release Note](https://github.com/oceanbase/seekdb/releases/tag/v1.3.0) 与 [`DBMS_INDEX_MANAGER.refresh`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/pl/sys_package/ob_dbms_index_manager.cpp) 的源码实现，业务可以根据场景自由权衡：默认 `sync_mode = async` 下前台写入飞快，若某个关键节点要求严格的“刚写完立刻就能被 ANN 搜出（Read-Your-Writes）”，只需在查询前显式调用 `DBMS_INDEX_MANAGER.refresh()`（底层阻塞等待 `change_stream_refresh_scn` 追平当前安全可见 SCN），或者直接将该表切为同步维护的 `immediate` 模式。

### 2.2 双层 HNSW、物理辅助表树与自适应混合过滤

为什么 seekdb 要把每个分区的向量索引固定拆成 `incr_data_`（接收 Change Stream 实时写入的内存增量小图）与 `snap_data_`（周期性后台构建的全量快照大图）**恰好两层**？

设想一下：如果在 HNSW 上直接做高频原地硬删点，很容易扯断小世界图的关键高速连边，导致后续查询召回率慢性退化；可如果学 LSM-Tree 每次内存满了就刷出一个新的不可变 HNSW 段文件，十几个段叠在一起，每次 Top-K 查询都要对十几张图分别做近邻搜索，延迟与 CPU 开销根本兜不住。固定成“增量小图 + 快照大图”两层后，**前台任意时刻的近邻查询扇出被死死钉在了常数 `2` 路**！

在 [`ObPluginVectorIndexAdaptor`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_adaptor.cpp) 与 [`sort_merge_delta_and_snap_vids()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_plugin_vector_index_util.cpp) 中，查询对两张图加读锁并行跑完 `knn_search()` 后，紧接着做一次带 `VID`（内部向量整数 ID）哈希判重的双指针归并：

```cpp
while (result_count < limit && i < delta_count && j < snap_count) {
  candidate = delta[i].distance <= snap[j].distance ? delta[i++] : snap[j++];
  if (!seen_vid(candidate.vid)) {
    output(candidate.vid, candidate.distance, candidate.extra_info);
  }
}
```

当然，“只有两张图”只是内存算子眼里的清爽视图。一旦跟进 SQL 代码生成器 [`ObTscCgService::generate_vec_idx_ctdef()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/code_generator/ob_tsc_cg_service.cpp) 和迭代器组装器 [`ObDASIterUtils`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/iter/ob_das_iter_utils.cpp)，你会看到优化器在底下把一次逻辑向量表扫描展开成了一棵精密咬合的**物理辅助表树**：

```cpp
vec_scan_ctdef->children_[0] = filter_or_index_scan; // 标量/全文预过滤子迭代器
vec_scan_ctdef->children_[1] = delta_buffer_ctdef;   // 增量缓冲辅助表（async 堆表下可跳过扫描）
vec_scan_ctdef->children_[2] = index_id_ctdef;       // 索引状态与删除可见性追踪表
vec_scan_ctdef->children_[3] = snapshot_data_ctdef;  // 快照持久化数据表
vec_scan_ctdef->children_[4] = main_table_ctdef;     // 用户主表回表取列与最终复核
vec_scan_ctdef->children_[5] = rowkey_vid_ctdef;     // 非整型主键下的 Rowkey <-> VID 双向映射表
```

而且整棵辅助表迭代器树在运行时拿到的是**同一个 SQL 事务描述符 `tx_desc_` 与同一个一致性读快照 `snapshot_`**！当一行数据被更新或删除时，旧 `VID` 哪怕暂时还挂在 `snap_data_` 大图里，也会被删除位图（Bitmap）、SCN 可见性校验、双路 `VID` 去重以及最后的统一快照回表层层拦下，绝不会变回“幽灵记录”溜回客户端。随着时间推移，[`ob_vector_index_refresh.cpp`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/vector_index/ob_vector_index_refresh.cpp) 中的后台 `refresh` 不断清理已沉淀的增量日志，周期性的 `rebuild` 则把累积的增量与删除彻底揉进新一代干净的 `snap_data_` 全量快照图里。

有了同一套 SQL 优化器兜底，开头提到的“标量 + 全文 + 向量”混合检索也就顺理成章了。在 [`ObDASHNSWScanIter::process_adaptor_state_hnsw()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/das/iter/ob_das_hnsw_scan_iter.cpp) 中，引擎支持按过滤条件选择率在三条路径间切换：高选择率小集合走 **Pre-Filter**（先由标量或全文倒排算出允许的 `VID` 位图传进 HNSW 内部剪枝），低选择率大集合走 **Post-Filter / Iterative Filter**（边做 ANN 搜索边回表过滤并按需扩充候选集直到凑够 `K` 条），不确定时则挂载 **Adaptive Path** 动态择优。甚至连兼容外部搜索习惯的 JSON 接口 `HYBRID_SEARCH('docs', '{...}')`，在 [`ObQueryTranslator::translate()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/hybrid_search/ob_query_translator.cpp) 与 [`ObDMLResolver::resolve_hybrid_search_item()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/dml/ob_dml_resolver.cpp) 里也是先被安全转译成带 `_score` 计算与 `APPROXIMATE` 标记的标准 `SELECT` 子查询，再二次解析喂给同一个 SQL 优化器——只维护一套执行引擎，就自动继承了全部事务快照与权限语义（需留意当前源码中该 JSON 入口要求目标表使用隐藏主键，对显式指定主键的表会返回 `OB_NOT_SUPPORTED`，而原生 SQL `ORDER BY l2_distance(...) APPROXIMATE` 则无此限制）。

---

## 三、Copy-on-Write 状态沙箱：宏块级 `FORK` 与主键对账 `MERGE` 的真实边界

解决了记忆的持续写入与混合检索，接下来看 Agent 最渴望的第三件武器：**怎样在不拷贝几十 GB 数据的前提下，给每次高风险推理实验切出一个随时可写、随时可丢的独立数据库沙箱？**

seekdb 把这个能力下沉成了两条原生 SQL 命令：`FORK TABLE / DATABASE` 与 `MERGE TABLE`。跟踪其完整生命周期，最能体会存储引擎层的硬核取舍：

![seekdb 面向 Agent 状态试错的存储级 COW FORK 与主键对账 MERGE 生命周期](/images/diagrams/aug-store-seekdb-cow-fork-merge.svg)

### 3.1 `FORK`：绑定辅助表快照与 SSTable 宏块级写时复制

试想一张挂着 HNSW 向量索引和全文索引的业务表正在承受高频并发写入，此时执行一条 `FORK TABLE docs TO docs_agent_branch`，如果只把主表元数据克隆一份而漏掉了底下的五六张向量/LOB 辅助表，或者主表与辅助表各自取了相差几毫秒的不同快照，切出来的沙箱当场就会发生主表与索引撕裂。

因此，RootService 侧的 [`ObForkTableUtil::obtain_snapshot()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/rootserver/fork_table/ob_fork_table_util.cpp) 第一步就是顺藤摸瓜，把主表、普通二级索引、向量/全文领域索引辅助表以及 LOB 大对象 Tablet 全部收拢进同一个列表，向全局时间戳服务（GTS）申请**唯一一个公共一致性快照 `snapshot_scn`**，并通过 `batch_acquire_snapshot(SNAPSHOT_FOR_DDL)` 死死锁住这些历史版本不被后台 Compaction 提前回收。

随后，[`ObForkTableTask`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/rootserver/fork_table/ob_fork_table_task.cpp) 推进 `PREPARE -> WAIT_FROZE_END -> BUILD_DATA -> WAIT_DATA_COMPLEMENT -> SUCCESS` 异步状态机，在存储层为每个 Tablet 拉起 [`ObTabletForkDag`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/storage/ddl/ob_tablet_fork_task.cpp)。翻开 DAG 的任务编排代码，就能看清“写时复制”在 LSM/SSTable 架构下的精确物理含义：

```cpp
for (ObSSTable *sstable : source_sstables) {
  bool fully_visible = sstable->is_major_sstable()
      ? sstable->get_end_scn() <= fork_snapshot
      : sstable->get_upper_trans_version() <= fork_snapshot;

  if (fully_visible) {
    prepare_task -> reuse_task(sstable) -> merge_task;   // 零拷贝：直接共享底层不可变宏块
  } else {
    prepare_task -> rewrite_task(sstable) -> merge_task; // 跨快照边界：扫描剔除未来版本后重写
  }
}
```

这里没有玄学：

- 对于所有事务版本均早于等于 `fork_snapshot` 的基线 Major SSTable 或历史 Minor SSTable，`reuse_task` 只需为目标沙箱 Tablet 生成一份新的 SSTable 元数据壳，**底层 2 MB 宏块（Macro Block）数据分文不动、原地零拷贝共享**；
- 但对于那些在抓快照前后恰好混杂了新写入、跨过了 `fork_snapshot` 边界的增量 SSTable，直接共享宏块就会让沙箱偷看到 Fork 之后的未来脏数据！此时 `rewrite_task` 必须拉起 `ObForkSnapshotRowScan`，逐行检查并跳过 `abs(trans_version) > fork_snapshot_version_` 的未来版本，把真正属于快照时刻的可见行重写进沙箱自己的新宏块里。

一旦挂载完毕，源表与沙箱表的新增修改各自写进独立的活跃 `MemTable`，共享的历史不可变宏块谁也不许原地涂改——假如 Agent 的十步重构实验把沙箱表改得一团糟、验证测试没过，一句 `DROP TABLE` 就能瞬间销毁沙箱独有的增量块，源表连一根毫毛都不会伤到。

### 3.2 `MERGE TABLE`：实用的主键二方对账，而非 Git 三方语义合并

既然 `FORK` 这么像 `git checkout -b`，那实验成功后执行 `MERGE TABLE docs_agent_branch INTO docs` 是不是就等于 `git merge`？

打开 [`ObMergeTableResolver`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/resolver/cmd/ob_merge_table_resolver.cpp) 和 [`ObMergeTableExecutor`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/cmd/ob_merge_table_executor.cpp) 的源码，答案非常明确：**不是。当前的 `MERGE TABLE` 完全没有读取 Fork 时刻的共同祖先（Common Ancestor / Base Version），而是在单个内部 SQL 事务里，直接对源分支表 `incoming` 与当前主表 `current` 做基于主键的二方状态对账（Primary-Key Reconciliation）。**

编译器在底层直接把 `MERGE TABLE` 翻译成了三段一目了然的普通 SQL 组合：

```sql
-- 1. FAIL 策略专属前置检查：一旦检出同主键且列值不等（NULL 安全比较），当场加锁报错中止
SELECT COUNT(*) AS conflict_cnt
FROM current c JOIN incoming i ON c.pk = i.pk
WHERE NOT (c.v1 <=> i.v1 AND c.v2 <=> i.v2)
FOR UPDATE;

-- 2. 所有策略通用（FAIL / THEIRS / OURS）：将沙箱中独有的新增主键插入主表
INSERT INTO current(pk, v1, v2)
SELECT i.pk, i.v1, i.v2
FROM incoming i LEFT JOIN current c ON i.pk = c.pk
WHERE c.pk IS NULL;

-- 3. 仅 THEIRS 策略执行：同主键但值不同时，强行用沙箱值覆盖主表（OURS 则跳过此步保留主表值）
UPDATE current c JOIN incoming i ON c.pk = i.pk
SET c.v1 = i.v1, c.v2 = i.v2
WHERE NOT (c.v1 <=> i.v1 AND c.v2 <=> i.v2);
```

用具体一行数据推演一下，你立刻就能摸准它的两个工程边界：

1. **没有共同祖先导致的“旧值倒灌”风险**：假设 `FORK` 时 `pk = 7` 的余额是 `100`；此后主表正常营业把 `pk = 7` 更新成了 `250`，而 Agent 在沙箱里压根没碰过 `pk = 7`（仍是 `100`）。此时如果盲目用 `THEIRS` 策略合入，由于对账 SQL 只看到 `250 != 100`，根本不知道哪一边才是真正动过手脚的一方，就会拿沙箱里陈旧的 `100` 把主表刚更新的 `250` 无情覆盖掉！因此在主表存在并发写的线上场景，务必优先使用 `FAIL` 策略拦截审查。
2. **沙箱物理 `DELETE` 不会反向删除主表行**：注意上面三段 SQL 里没有任何 `DELETE` 语句！如果在沙箱里删掉了 `pk = 9`，在对账视角下 `pk = 9` 只是“在 `current` 里存在、在 `incoming` 里不存在”，与“Fork 之后主表新插入了一行 `pk = 9`”长得一模一样，系统绝不敢贸然删主表。如果 Agent 业务需要在沙箱里表达删除意图并合回主表，必须使用软删除标记列（如 `is_deleted = 1`）走 `UPDATE` 通道传播。

---

## 四、嵌入式形态、库内模型治理与七条跨层不变量

除了核心存储与索引链路，seekdb 在部署形态和 AI 模型调用上也做了两处贴合 Agent 工程痛点的设计。

第一处是 **Embedded 嵌入式模式**（[`src/observer/embed/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/embed)）。本地桌面 Agent 或 Python CLI 工具最怕为了跑一个状态库还得先装 Docker 拉起一套分布式集群。[`seekdb.h`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/embed/c/seekdb.h) 提供了类似 SQLite 的 `seekdb_open / connect / execute` C/Python 接口，省去了独立守护进程与 MySQL 协议网络往返（直接走进程内 `ObInnerSQLConnection` 连接池）；但看清 `ObLiteEmbed::do_open_()` 就会知道，它在进程内拉起的依然是一整套货真价实的 `OBSERVER` 内核（默认申请 `1G` 内存上限与 `2G` 日志盘目录，等待 `root_service_->is_full_service()` 就绪后返回）。这种“重内核嵌入”换来了本地开发与云端 Server 百分之百一致的 SQL、向量与 FORK 语义，但首开启动耗时与内存底噪远高于轻量文件库；此外，当前版本进程内全局单例 `GCTX` 只允许打开一个实例，且底层 `ObLiteEmbed::close()` 仍带 `_Exit(0)`，C 包装层 `seekdb_close()` 为免误杀宿主进程暂仅释放连接句柄而留待后续补齐优雅停机。

第二处是 **库内 AI Service 模型治理**（[`src/observer/ai_service/`](https://github.com/oceanbase/seekdb/tree/1e9b720c/src/observer/ai_service)）。seekdb 没有在 C++ 存储内核里硬塞庞大的 GPU 推理运行时，而是通过 `DBMS_AI_SERVICE` 在数据库 Catalog 内统一托管外部 Dense/Sparse Embedding、Rerank 与 Completion 端点凭证，并拆出快慢两条调用通道：普通 SQL 表达式里的 [`AI_EMBED()`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/sql/engine/expr/ob_expr_ai/ob_expr_ai_embed.cpp) 走同步 HTTP 请求直出向量；而大批量建索引与后台刷新则走 [`ObEmbeddingTask`](https://github.com/oceanbase/seekdb/blob/1e9b720c/src/observer/vector_index/ob_vector_embedding_handler.cpp)，利用 `cURL multi` 异步状态机（`INIT -> HTTP_SENT -> HTTP_COMPLETED -> PARSED -> DONE`）做分批并发、指数退避重试与维度校验，避免外部模型 API 抖动拖死数据库内核线程。

把全篇跨模块的源码线索收拢起来，seekdb 之所以能把关系事务、实时 HNSW 与状态沙箱捏成一个自洽整体，靠的是底下**七条环环相扣的跨层不变量**：

1. **主表事务先行**：未提交或已回滚的 Redo 严禁越过 `ObCSFetcher` 污染异步索引可见水位；
2. **局部回滚精确重放**：`ROLLBACK TO SAVEPOINT` 区间必须在生成索引事件前被完整剥离；
3. **单批次 Schema 纯净**：同一个 Change Stream Batch 绝不横跨两个 `schema_version_` 解码行字节；
4. **并行建图、串行水位**：Worker 图计算可多核并行，但 `change_stream_refresh_scn` 必须严格顺着事务环序号单调推进，任一批次失败即升级 Epoch 整体重放；
5. **消费下界锁住日志回收**：`change_stream_min_dep_lsn` 时刻锚定最早未决事务起点，防止底层日志先于索引消费被截断；
6. **常数两路归并遮蔽旧点**：`incr_data_` 与 `snap_data_` 两路候选必须经删除位图、SCN 与 `VID` 哈希去重联合清洗；
7. **回表与分支共享快照**：无论是 ANN 检索跨多张辅助表回主表，还是 `FORK TABLE` 克隆主表与索引表簇，均强制绑定同一个事务/GTS 快照时刻。

放到更大的现代数据系统坐标系里，seekdb 与云数仓（如 [BigQuery Vector Search](https://cloud.google.com/bigquery/docs/vector-search-intro)、[Snowflake Cortex Search](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview)、[Databricks Mosaic AI Vector Search](https://docs.databricks.com/en/generative-ai/vector-search.html)）以及学术界的流式图索引（[FreshDiskANN](https://arxiv.org/abs/2105.09613)、[SPFresh](https://doi.org/10.1145/3600006.3613166)）走的是互补分野：云数仓长于 PB 级列式扫描、离线特征清洗与全托管弹性算力；而 seekdb 则把准星牢牢对准了**单机/嵌入式低延迟高频事务写、在线混合检索与轻量级可写状态分支**。展望下一代 AI-Native 数据库内核，若能进一步把新鲜度水位（`as-of watermark` / `read-your-writes`）提炼为 SQL 级声明式语法、把向量召回率与外部模型 Token 延迟纳入优化器统一代价模型，并为 `FORK / MERGE` 补齐保存共同祖先与删除墓碑的真正三方语义合并（Three-Way Lineage Merge），这套架构还将释放出更大的想象空间。

---

## 源码阅读顺序与基线说明

面对体量庞大的 C++ 数据库代码库，建议紧抓 `tx_desc`、`schema_version`、`SCN/LSN` 与 `tablet_id` 四个核心身份标识，按下面八个因果阶梯递进阅读：

| 阅读阶梯 | 起点文件 | 收束文件 | 核心核验问题 |
| --- | --- | --- | --- |
| 1. 接入与嵌入启动 | `ob_server.cpp`、`ob_embed_impl.cpp` | `obmp_query.cpp`、`ObInnerSQLConnection` | Embedded 与 Server 模式共享了哪些内核组件？ |
| 2. SQL 主表写路径 | `ob_sql.cpp` | `ob_das_insert_op.cpp`、`ob_access_service.cpp` | 一条 `INSERT` 如何定位 LS 与目标 Tablet？ |
| 3. MVCC 与日志回调 | `ob_tablet.cpp` | `ob_memtable.cpp`、`ob_mvcc_trans_ctx.cpp` | 新旧行镜像何时由回调打包进 Palf Redo？ |
| 4. Change Stream | `ob_change_stream_fetcher.cpp` | `dispatcher`、`worker`、`ob_cs_plugin_async_index.cpp` | Savepoint 过滤、串行水位推进与 Epoch 重试怎样闭环？ |
| 5. 向量扫描计划展开 | `ob_log_table_scan.cpp` | `ob_tsc_cg_service.cpp`、`ob_das_hnsw_scan_iter.cpp` | 逻辑向量扫描如何展开为 6 张物理辅助表迭代器树？ |
| 6. 双层 HNSW 内存层 | `ob_plugin_vector_index_adaptor.cpp` | `ob_plugin_vector_index_util.cpp`、`ob_vector_index_refresh.cpp` | 增量/快照双路归并、位图遮蔽与后台重建如何协同？ |
| 7. Hybrid JSON DSL | `ob_query_parse.cpp` | `ob_query_translator.cpp`、`ob_dml_resolver.cpp` | JSON 搜索请求如何安全转译回 SQL 子查询计划？ |
| 8. COW 分支与对账 | `ob_fork_table_util.cpp` | `ob_tablet_fork_task.cpp`、`ob_merge_table_resolver.cpp` | 宏块级物理 COW 与二方主键对账各自停在哪道边界？ |

- **源码基线**：[oceanbase/seekdb](https://github.com/oceanbase/seekdb) 仓库 `develop` 分支 `v1.1.0-572-g1e9b720c`（提交 [`1e9b720c`](https://github.com/oceanbase/seekdb/commit/1e9b720c)），新鲜度同步模式契约交叉核对了官方 [v1.3.0 Release Note](https://github.com/oceanbase/seekdb/releases/tag/v1.3.0) 及仓库内 `fork_table` 测试集。需要留意：存储层 `FORK` 提供的是数据结构隔离分支，并非隔离恶意代码访问宿主机文件或网络的操作系统安全沙箱。

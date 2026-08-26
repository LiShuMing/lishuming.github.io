---
title: "【源码】深入表达式下推：StarRocks、Doris 与 Databend 如何减少 Scan I/O"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
slug: "dive-expression-pushdown"
categories:
  - 数据库
tags:
  - Predicate Pushdown
  - Runtime Filter
  - StarRocks
  - Apache Doris
  - Databend
  - 存算分离
description: "从存储执行层出发，对比 StarRocks、Apache Doris 与 Databend 的元数据裁剪、Bloom Filter、Runtime Filter、延迟物化和对象存储 I/O 调度实现。"
draft: false
---

存算分离把计算节点从本地磁盘中解耦出来，带来了弹性伸缩、资源隔离和更低的长期存储成本，也改变了 Scan 的代价结构：一次无效读取不再只是磁盘带宽问题，还可能包含对象存储请求、网络传输、本地缓存填充、解压和解码等多层开销。

因此，“把谓词下推到存储层”是正确方向，但它不是一个单独的开关。真正需要回答的是：**谓词能够下推到哪一级？该级别能跳过多少物理数据？为了做出跳读判断，又额外读取了多少索引并发出了多少请求？**

本文沿着这一问题，对 StarRocks、Apache Doris 和 Databend 的源码进行横向分析。讨论范围刻意排除优化器阶段的 Partition Pruning 和 Bucket Pruning，聚焦 Scan 内部的 Segment、Row Group、Block、Page 和 Column Chunk，并重点回答以下问题：

1. 静态谓词如何借助有序键、Min/Max、Bloom Filter 和二级索引减少 I/O？
2. 当谓词来自 Join 的 Runtime Filter 时，特别是 Bloom Filter，哪些优化真的发生在数据读取之前？
3. 当元数据无法排除数据时，延迟物化如何减少无效列读取？
4. 除此之外，存储执行层还有哪些降低对象存储成本的手段？

## 背景与核心观点

先给出本文的核心判断，后续源码分析都围绕这些判断展开：

1. **谓词下推是一个逐层收缩候选集的漏斗，而不是“下推或不下推”的二元状态。** 文件级、Row Group/Block 级和 Page 级裁剪能直接避免数据 I/O；行级向量化过滤通常只能减少后续解码、物化和算子计算。
2. **Min/Max 的效果主要取决于数据布局，而不是索引本身。** 数据在过滤列上越聚簇，Zone Map 越容易排除整块；随机分布会让每个块的 `[min, max]` 接近全局范围，使索引快速退化。
3. **持久化 Bloom 与 Runtime Bloom 不是同一种东西。** 前者总结“某个文件块中可能有哪些值”，后者总结“本次 Join Build 侧可能有哪些键”。只有哈希、布局、类型编码和粒度满足严格兼容关系时，二者才可能直接组合；三套引擎的通用路径都没有把二者简单等同。
4. **Runtime Filter 最容易下推到元数据层的是 MinMax 或小型 InList。** StarRocks 把范围摘要转成只用于索引裁剪的谓词，再查询 Zone Map；Doris 将 IN/MinMax 归一为 Storage Predicate 或 Key Range；Databend 用 MinMax/InList 裁剪 Block，并在 InList 足够小时查询持久化 Bloom 索引。
5. **运行时 Bloom 的主要价值通常是尽早过滤行，并与延迟物化配合。** 先读取 Join Key/过滤列，使用 Runtime Bloom 生成 Selection，再读取存活行的宽列，仍然可以显著减少 Payload I/O，但它与“完全不读 Probe 列”是两种不同收益。
6. **对象存储场景不能只追求最少字节数。** 过度稀疏的 Range Read 会产生大量小请求；I/O Coalescing 会主动多读少量字节以减少请求次数。工程目标应是最小化总成本，而不是孤立地最小化 `BytesRead`。

可以把上述观点进一步收束为一个判断标准：**只有在数据请求发出前缩小物理读取范围，或在读取窄 Probe 列后阻止宽 Payload 列读取，谓词下推才真正转化为 Scan I/O 收益。** 仅把表达式移动到 ScanNode、减少返回行数，或者降低上层算子 CPU，都不能单独证明远端读取已经下降。

本文使用本地源码逐项核对，分析基线如下：

| 项目 | 源码快照 | 日期 | 重点路径 |
|------|----------|------|----------|
| StarRocks | [0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 2026-03-30 | Native Segment、Parquet Reader、Runtime Range Pruner |
| Apache Doris | [5202d06](https://github.com/apache/doris/tree/5202d06dd8feb3390ff32839227eeee89c345b57) | 2026-08-21 | SegmentIterator、VParquet Reader、Runtime Filter Consumer |
| Databend | [ab6f27c](https://github.com/databendlabs/databend/tree/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456) | 2026-05-27 | Fuse Pruner、Prewhere、Runtime Filter Pruner |

由于三个项目都在快速演进，本文描述的是这些快照中的实现，不把类名、配置项或 TODO 外推为所有历史与未来版本的固定行为。

本文使用以下术语区分优化发生的位置：

| 层级 | 常见结构 | 能否避免数据 I/O | 典型代价 |
|------|----------|------------------|----------|
| 文件/Segment | Footer、Segment Zone Map、短键索引 | 可以，跳过整个文件或 Segment | 读取 Footer、元数据缓存查询 |
| Row Group/Block | Min/Max、Null Count、Bloom、Block Meta | 可以，跳过整组数据 | 索引文件或对象存储元数据请求 |
| Page | Page Index、Page Zone Map、Page Bloom | 可以，跳过部分 Page/Column Chunk | 更细索引、更多 Range |
| 行位置 | Bitmap、Inverted Index、Selection、RowId | 可以减少后续列读取，但索引列可能已读取 | 位图计算、稀疏读取 |
| 行值 | 向量化谓词、Runtime Bloom Probe | 不能撤销当前列的读取，可减少后续工作 | Hash、表达式计算 |

这里的“避免 I/O”还需要进一步细分：命中本地 Data Cache 时，没有远端读取但仍有内存拷贝和解码；跳过 Payload 列时，谓词列已经发生 I/O；合并 Range 时，远端请求减少但字节数可能增加。本文会在具体实现中区分这些情况。

## 一条统一的谓词下推链路

### 从 SQL 表达式到物理候选范围

一条 SQL 谓词进入存储层后，通常会经历四种表示：

```text
SQL Expr
   │  语义分析、常量折叠、类型转换
   ▼
执行表达式 / Predicate Tree
   │  提取单列条件、判断索引能力
   ▼
元数据谓词
   │  Min/Max、Bloom、Dictionary、Bitmap、Inverted Index
   ▼
候选行范围
   │  SparseRange / RowRanges / Bitmap / Partitions
   ▼
读取谓词列 → 计算 Selection → 延迟读取输出列 → Residual Predicate
```

元数据判断必须是保守的：**允许误报，不允许漏报。** 当索引无法证明一个块一定不匹配时，只能保留该块，并在读取真实值后执行残余谓词。于是，索引裁剪结果本质上是候选集的上界，而不是 SQL 条件的最终结果。

### 元数据裁剪的能力边界：Min/Max 与 Bloom

Min/Max 与 Bloom Filter 都能在读取数据页之前做否定判断，但两者解决的是不同问题。

**Min/Max 的效果依赖数据聚簇。**

设 Page `P` 在列 `x` 上记录范围：

```text
P.min = 100
P.max = 199
```

对于 `x >= 500`，该 Page 可以安全跳过；对于 `x = 150`，只能判断“可能存在”。如果数据按 `x` 有序或近似聚簇，连续 Page 的范围通常互不重叠，裁剪能力很强；如果值随机散布，每个 Page 都可能覆盖很宽的范围，Min/Max 很难证明 `MustFalse`。

所以 Sort Key、Cluster Key、Compaction 和 Recluster 不只是写入侧功能，它们决定了轻量级元数据能否在读取侧发挥作用。

**Bloom Filter 解决的是存在性问题，而不是范围问题。** 它适合回答“某个值是否一定不存在”：

- 返回 `false`：一定不存在，可以安全跳过；
- 返回 `true`：可能存在，需要继续读取；
- 不保存顺序，不适合直接处理任意范围条件；
- 误报率取决于位图大小、哈希数量和不同值数量。

因此，持久化 Bloom 通常服务于 `=`、部分 `IN` 等点查询，而 Zone Map 更适合范围查询。好的 Scan 不会二选一，而是先用便宜元数据缩小范围，再判断读取 Bloom 的成本是否值得。

### 延迟物化：元数据无法命中时的第二道防线

当元数据无法排除 Page 时，最直接的做法是读取所有投影列，再计算过滤条件；宽表中这会把大量最终被丢弃的 Payload 列读入内存。

延迟物化将读取拆成两阶段：

```text
第一阶段：读取 predicate / join-key 列
          │
          ├── 计算静态谓词
          ├── 计算 Runtime Filter
          └── 生成 RowId / Selection / Bitmap
                         │
第二阶段：只读取存活行的 payload / output 列
```

它的收益近似取决于 `PayloadBytes × 过滤率`，代价则包括二次 Seek、稀疏读取、位置列表和列合并。当谓词几乎不过滤、Payload 很窄或远端小请求非常昂贵时，提前一次性读取可能更快。因此，三套引擎都能看到选择率阈值、列读取顺序或 I/O 合并方面的自适应设计。

## StarRocks——以 SparseRange 为中心的多级裁剪

### Native Segment：把不同索引归一为 SparseRange

StarRocks Native Reader 的核心入口是 [`SegmentIterator::_init_internal()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp)。源码特意注明调用顺序不可随意修改，过滤链路依次包括：

```text
完整 Segment 行范围
  → RowId Range
  → Short Key / Sorted Key
  → Tablet Range
  → Delete Vector（可配置延后）
  → Bitmap Index
  → Zone Map
  → Bloom Filter
  → Inverted Index
  → Vector Index / Sampling
  → 最终 SparseRange
```

每一级都把候选范围与当前 `_scan_range` 取交集。这样做有两个重要含义：

1. 后续索引只需处理前一级留下的范围，避免重复扫描完整索引空间；
2. 最终读取器面对的是统一的 `SparseRange`，上层不需要理解候选范围来自哪种索引。

短键索引在有序键空间上定位起止 RowId；Zone Map 和 Bloom Filter 下沉到 Page；Bitmap/Inverted Index 直接产生更细的行集合。它们的粒度与适用谓词不同，但最终都转化为“哪些物理位置仍需读取”。

**静态 Bloom 的位置。**

[`_get_row_ranges_by_bloom_filter()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp) 先使用 `BloomFilterSupportChecker` 判断 Predicate Tree 是否存在可用节点，再由 `BloomFilterEvaluator` 收缩 `_scan_range`。这里面对的是文件写入时生成的 Page Bloom，适用于可以提取常量探测值的静态谓词。

Bloom 并非免费：需要读取索引页并执行 Hash。若前面的 Sorted Key、Zone Map 已经把范围缩得很小，额外读取 Bloom 可能得不偿失。因此，索引顺序和代价控制同样重要。

### Runtime Filter：范围裁剪与行级过滤两条路径

StarRocks 的实现最值得注意的地方，是没有把 Runtime Filter 当成一种单一谓词。

第一条路径位于 [`runtime_range_pruner.hpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_range_pruner.hpp)：

1. 从已经到达的 Runtime Filter 中提取 `InFilter` 和 `MinMaxFilter`；
2. 构造 `ColumnValueRange`；
3. 转换为 Storage `ColumnPredicate`；
4. 标记 `index_filter_only`；
5. 用这些谓词查询 Zone Map，并与剩余 `_scan_range` 求交。

[`_try_to_update_ranges_by_runtime_filter()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp) 会在扫描过程中检查晚到的过滤器，用范围摘要继续收缩尚未读取的数据，并累计 `runtime_stats_filtered`。

`index_filter_only` 是一个关键语义：假设 Join Build Key 是 `{1, 100}`，其 MinMax 为 `[1, 100]`。Zone Map 可以用这个范围排除 `[200, 300]` 的 Page，却不能认为值 `50` 一定能 Join。范围谓词只是索引裁剪的必要条件，最终正确性仍由 Runtime Filter 或 Join 保证。

第二条路径位于 [`runtime_filter_predicate.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_filter_predicate.cpp)：真实 Runtime Filter 在读取 Probe Column 后生成 Selection。实现还会：

- 采样每个 Runtime Filter 的过滤率；
- 最多保留三个有效过滤器，过滤率极高时只保留最佳者；
- 根据 Selection 的稀疏程度选择 Branchless 或 Selection 执行模式；
- 对全字典编码列，把 Runtime Filter 先作用于字典词，再用字典码过滤每一行。

这些优化主要减少 Hash、字符串解码和后续物化 CPU；只有与范围裁剪或延迟物化结合时，才进一步转化为远端 I/O 收益。

### Runtime Bloom 的能力边界：不能跳过 Probe 列，但能跳过 Payload

[`PredicateLateMaterializationScanStrategy::read_columns()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp) 中有一条非常直接的注释：`TODO: support runtime bloom filter push down to page level`。当前快照中，如果第一谓词列带 Runtime Filter，会避开该列原有的 Page Predicate Pushdown 快速路径，先读取列值再执行 Runtime Filter。

这不是简单遗漏，而是两类 Bloom 的兼容问题：

| 维度 | 持久化 Page Bloom | Runtime Bloom |
|------|-------------------|---------------|
| 表示集合 | 当前数据 Page 的值 | 本次 Join Build 侧的键 |
| 生命周期 | 写文件时生成，长期存在 | 查询执行时生成，查询结束即释放 |
| 粒度 | Page/Row Group/Block | Join/Fragment/Partition/Pipeline |
| 编码与 Hash | 文件格式固定 | 执行引擎内部协议 |
| 目标 | 证明某个常量一定不在数据块中 | 尽早排除一定无法 Join 的 Probe 行 |

理论上，如果两个 Bloom 使用完全相同的类型规范化、哈希、位图布局和参数，可以设计保守的相交判定；但这不是通用 Bloom 接口天然保证的能力。当前实现选择用 Runtime MinMax/IN 摘要裁剪元数据，并用 Runtime Bloom 过滤真实 Probe 值，正确性边界更清晰。

不过，“不能直接利用 Page Bloom”不等于“不能减少 Scan I/O”。StarRocks Native Segment 已经把 Runtime Bloom 接入延迟物化链路，关键调用都位于 `segment_iterator.cpp`：

```text
_build_column_oriented_rf()
  → 将 Runtime Filter 按 ColumnId 归入谓词列
  → _predicate_evaluate_late_materialize_read_first_column()
      先读第一批谓词列和 RowId
  → _filter_by_compound_and_predicates()
      在真实 Probe 值上执行 Runtime Bloom，压缩 Selection/RowId
  → _evaluate_late_materialize_read_other_columns()
      只为存活 RowId 读取其余谓词列
  → _finish_late_materialization()
      通过 decode_values_by_rowid() 读取存活行的输出列
```

明确结论是：**Runtime Bloom 可以与延迟物化结合，并减少 Scan I/O；它节省的是被过滤行对应的 Payload/Output Column I/O，而不是 Runtime Bloom 所依赖的 Probe/Join-Key Column I/O。** 如果 Join Key 很窄、Payload 很宽且 Bloom 过滤率高，收益会非常明显；如果查询只投影 Join Key，或者 Bloom 几乎不过滤，则字节收益很小，甚至可能被额外 Hash 成本抵消。

对于多个谓词列，Segment Scan 还会采样实际选择率并调整读取顺序，使过滤能力更强的列尽量提前。这里形成了完整的两级收益：Runtime MinMax/IN 先尝试缩小 Page 范围，Runtime Bloom 再读取 Probe Key 并压缩 RowId，最后延迟读取宽列。

**Parquet 路径采用相似但更贴近文件格式的实现：**

- [`PredicateFilterEvaluator`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/predicate_filter_evaluator.h) 依次尝试 Row Group Statistics、Page Index 和 Row Group Bloom；
- [`GroupReader`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/group_reader.cpp) 把列拆为 Active 与 Lazy 两组；
- Active 列逐列读取和过滤，命中数为零时立即停止；
- Lazy 列只读取 Selection 覆盖的范围，再与 Active Chunk 合并；
- 字典谓词可在字典上计算一次，必要时直接跳过整个 Row Group。

对象存储上还有一个看似矛盾但合理的优化：`io_coalesce_adaptive_lazy_active` 根据此前 Lazy Column 是否真正被需要，决定 Active/Lazy 的 I/O Range 一起合并还是分开。一起读取会多读部分 Payload，却减少请求；分开读取更节省字节，但可能增加远端往返。

## Apache Doris——Bitmap Row Set 与两阶段列读取

### Native Segment：统一候选位图与 Runtime Filter 表示

Doris 的核心入口是 [`SegmentIterator::_lazy_init()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/segment_iterator.cpp)。它先把整个 Segment 加入 `_row_bitmap`，再依次应用：

```text
Short Key Range
  → Inverted / Expression Index
  → Dictionary
  → Bloom Filter Index
  → Zone Map
  → Delete Bitmap / Row Range
  → ANN TopN
  → 最终 Roaring Bitmap
```

与 StarRocks 的 `SparseRange` 类似，Doris 用 Roaring Bitmap 统一表达不同索引留下的候选行。Zone Map 和 Bloom 返回 Page 对应的 RowRanges，随后转换为 Bitmap 并求交；Inverted Index 可以直接返回更精细的命中位图。

Runtime Filter 可以进入这条存储谓词链路，但能否转化为读前裁剪，取决于它的具体表示。答案并不是笼统的“可以”或“不可以”：

1. [`RuntimeFilterConsumer::_get_push_exprs()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/runtime_filter/runtime_filter_consumer.cpp) 将 Runtime IN、MinMax 和 Bloom 分别生成为 `IN_PRED`、`BINARY_PRED` 和 `BLOOM_PRED`；
2. [`ScanLocalState::_normalize_predicate()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/scan_operator.cpp) 解开 Runtime Filter Wrapper，再尝试归一为 `ColumnValueRange` 和 Storage `ColumnPredicate`；
3. Runtime IN 可以形成 Scan Key、值域或 Storage IN Predicate，Runtime MinMax 可以形成范围条件，因而有机会复用 Short Key、Zone Map、Parquet Statistics/Page Index 等读前裁剪能力；
4. Runtime Bloom 被归一为 `BloomFilterColumnPredicate`，主要在读取真实 Probe Column 后执行，并通过延迟物化减少后续列读取。

[`ColumnReader::get_row_ranges_by_bloom_filter()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/column_reader.cpp) 会先借助 Ordinal Index 找到当前候选范围覆盖的 Page，再逐页读取持久化 Bloom；只有谓词树判断“可能命中”的 Page 才加入新范围。这个顺序避免了为已经被其他索引排除的 Page 再加载 Bloom。

**Runtime Bloom 主要执行在真实列值上。** Doris 将其构造成 [`BloomFilterColumnPredicate`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/predicate/bloom_filter_predicate.h)，能够对普通列和字典码列执行向量化过滤，并通过采样判断低选择率 Runtime Filter 是否值得继续计算。

`BloomFilterColumnPredicate` 的核心实现是对已经解码出的 `IColumn` 调用 Runtime Bloom，而不是直接比较 Runtime Bloom 与文件中的 Page Bloom。所谓“仍需读取 Probe Column”，指的是 Scan 必须先把 Join Key 所在的 Column Page 读入并解码，才能逐值执行 `find()`；这部分 I/O 已经发生，无法被同一个 Runtime Bloom 撤销。它能做的是先产生更稀疏的 Selection，再阻止被过滤行的宽 Payload 列进入读取链路。

**等待时间是过滤收益与 Pipeline 阻塞之间的显式权衡。** 在该源码快照中，[`SessionVariable.runtimeFilterWaitTimeMs`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/qe/SessionVariable.java) 的默认值为 `1000` 毫秒。`RuntimeFilterConsumer` 的实际规则是：Descriptor 指定值优先；远端 Runtime Filter 默认等待 1000ms；开启无限等待或不存在远端目标时，等待上限取查询 `execution_timeout`。等待超时后 Scan 会继续运行，晚到的过滤器只能影响尚未读取的数据。因此，1000ms 是默认折中值，不是所有查询的最优值。

### Native 与 Parquet：Runtime Filter 如何驱动两阶段列读取

[`_vec_init_lazy_materialization()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/segment_iterator.cpp) 将列分为三类：

- `_predicate_ordinals`：先读并计算谓词；
- `_common_expr_ordinals`：过滤后再读，用于剩余公共表达式；
- `_output_ordinals`：最后按存活 RowId 读取。

`_next_batch_internal()` 先从最终 `_row_bitmap` 中取得一批 RowId，读取谓词列并计算 Selection；如果仍有存活行，再通过 `_read_columns_by_rowids()` 读取表达式列和输出列。对于 Struct/Array/Map，还能把谓词需要的子路径与最终输出需要的子路径拆开，过滤后再恢复被延迟的嵌套字段。

这里体现了一个实用的成本判断：定长数值列顺序读取和 SIMD 过滤很便宜，过度 Lazy Read 的 Seek 成本可能更高；字符串、Bitmap、HLL 等宽列则更容易从延迟物化获益。源码注释也明确把“读取速度”和“谓词计算速度”作为不同维度处理。

**Parquet 路径。** Doris 外部 Parquet 读取链路集中在 [`vparquet_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/format/parquet/vparquet_reader.cpp) 与 [`vparquet_group_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/format/parquet/vparquet_group_reader.cpp)：

1. Row Group 级先尝试 Column Statistics 和 Bloom Filter；
2. 未能排除时读取 ColumnIndex/OffsetIndex，生成 Page 对应的 `candidate_row_ranges`；
3. 创建 Column Reader 前，把 Position Delete、Deletion Vector 和缓存条件并入范围；
4. 先读 Predicate Columns，计算 `FilterMap`；
5. 再让 Lazy Columns 按 FilterMap 跳过无效行；
6. 对全字典编码 Column Chunk，可在字典上重写谓词并跳过整个 Row Group。

`_generate_random_access_ranges()` 还会收集所需 Column Chunk 的物理范围；小 I/O 可交给 `MergeRangeFileReader` 合并。这说明 Parquet 优化不只发生在逻辑候选行层，也发生在实际 Range Request 的组织层。

Runtime Filter 对这些能力的复用同样分成两类。`_collect_predicate_columns_from_conjuncts()` 会识别并解开 Runtime Filter Wrapper，把 Probe Slot 加入 Predicate Columns：Runtime IN/MinMax 在归一化成功时可参与 Row Group Statistics、Page Index 等元数据裁剪；Runtime Bloom 本身则成为先读的 Predicate Column 条件，生成 `FilterMap`，让 Lazy Columns 跳过无效行。换言之，**Doris 能让 Runtime Filter 贯穿 Parquet 的“元数据裁剪—谓词列读取—Lazy Column 读取”链路，但不同 Runtime Filter 形态进入的层级不同。**

## Databend——Fuse Block Pruning 与 Prewhere

### Fuse Pruner：独立的并行裁剪阶段

Databend Fuse Engine 的 [`PruningContext`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/fuse_pruner.rs) 在扫描前准备多种 Pruner：

| Pruner | 信息来源 | 作用 |
|--------|----------|------|
| Range Pruner | Segment/Block Column Statistics | 用表达式 Domain 判断 `MustFalse` |
| Bloom Pruner | 独立 Bloom Index | 处理 Equality、InList 等点查询 |
| Page Pruner | Native Page Meta、Cluster Key | 在 Block 内继续缩小 Page |
| Inverted Index Pruner | 倒排索引 | 生成匹配行集合 |
| TopN Pruner | Order/Limit 与统计信息 | 跳过不可能进入 TopN 的 Block |
| Virtual/Spatial Pruner | 虚拟列或空间索引元数据 | 避免读取源列或无关空间块 |

[`FusePruner::pruning()`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/fuse_pruner.rs) 的主干是 `Segment Pruner → Block Pruner → TopN Pruner`。Block Meta 可以通过 Cache 复用，Pruning 使用独立 Runtime 与 Semaphore 控制并发，避免为了读索引而无界放大对象存储请求。

### Runtime Filter：不同表示服务不同裁剪层级

Databend 的 Hash Join Build 侧会根据阈值构造 MinMax、InList、Bloom 或 Spatial Runtime Filter，见 [`RuntimeFilterLocalBuilder`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/service/src/pipelines/processors/transforms/hash_join/runtime_filter/local_builder.rs)。跨节点合并后，同一个 Runtime Filter 可以映射到多个 `(probe_key, scan_id)` 目标。

Probe 侧不是让所有结构做同一件事：

- [`RuntimeStatsPruner`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/expr_runtime_pruner.rs) 用 MinMax/InList 表达式与 Block Column Statistics 做 Domain Fold；结果恒为 `false` 时跳过整个 Block；
- `ExprRuntimePruner` 在 Runtime InList 值数量不超过阈值时，还会调用持久化 Bloom Index；
- Runtime Bloom 则进入 Prewhere/Preread 列集合，读取真实 Probe Column 后生成 Bitmap；
- Native Reader 可以在 Page 阶段先应用 Bloom Runtime Filter，若整页无命中则不读剩余列。

Databend 的设计给出了一个很清晰的分工：**MinMax/InList 负责“读之前能否排除 Block”，Runtime Bloom 负责“读到 Probe Key 后还能否避免剩余列”。** 小型 InList 是两者之间的桥梁，因为它既能作为精确表达式查询 Statistics，也能逐值查询持久化 Bloom。

### Prewhere：在过滤收益、稀疏读取与等待之间取舍

[`ReadState`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/read_state.rs) 把 Prewhere Column 和 Runtime Bloom Column 合并为 Preread Projection，再计算静态 Filter Bitmap 与 Runtime Filter Bitmap。

当选择率达到配置阈值时，Bitmap 转换为 `RowSelection` 并下推给 Remaining Column Reader；否则先顺序反序列化剩余列，再在内存中过滤。这避免了低选择性场景中“为了少丢几行，却制造大量稀疏 Seek”的反优化。

Native Reader 的 [`native_data_source_deserializer.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/native_data_source_deserializer.rs) 进一步按 Page 循环：TopK → Prewhere → Runtime Bloom → Remaining Columns。任一阶段判断当前 Page 全部不匹配，就直接进入下一组 Page。

[`runtime_filter_wait.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/runtime_filter_wait.rs) 在该快照中以 50ms 轮询 Runtime Filter Ready，最长等待 30 秒，同时监听查询取消和下游结束。

这说明 Runtime Filter 下推不仅是过滤算法，也是 Pipeline 调度问题：

```text
等待更久  →  更可能在 Block Pruning 前得到过滤器  →  少读数据，但增加空等
立即扫描  →  更快产生首批数据                 →  晚到过滤器只能影响剩余部分
```

对于高延迟对象存储和高选择率 Join，等待通常更有价值；Build 很大或过滤率很低时，等待可能比节省的 I/O 更贵。

## 横向对比

| 能力 | StarRocks | Apache Doris | Databend |
|------|-----------|--------------|----------|
| Native 候选集合 | `SparseRange` | Roaring `_row_bitmap` | Segment/Block `PartInfo`、Page Selection |
| 有序键裁剪 | Short Key / Sorted Key | Short Key / Key Range | Cluster Key + Range/Page Pruner |
| Min/Max 粒度 | Segment/Page Zone Map；Parquet Row Group/Page | Segment/Page Zone Map；Parquet Row Group/Page | Segment/Block Statistics；Native Page Meta |
| 持久化 Bloom | Native Page、Parquet Row Group | Native Page、Parquet Row Group | 独立 Block Bloom Index |
| Runtime MinMax/IN | 转 Index-only Predicate，继续查询 Zone Map | 可形成 Storage Predicate/Key Range，受类型和谓词形态约束 | 直接用 Statistics，阈值内 InList 还查询持久化 Bloom |
| Runtime Bloom | Probe Column 行级过滤；可驱动延迟物化 | `BloomFilterColumnPredicate` 行级过滤；可驱动延迟物化 | Preread/Prewhere 行或 Page 过滤，再读剩余列 |
| 延迟物化 | Predicate Column → RowId → Output Column；Parquet Active/Lazy | Predicate/Common Expr → RowId → Output；Parquet Predicate/Lazy | Prewhere/Runtime Key → RowSelection → Remaining Column |
| 字典优化 | Runtime Filter 先过滤字典词；静态字典谓词改写 | 字典列直接执行 Runtime Bloom；Parquet 字典过滤 | Prewhere/表达式框架内避免不必要的剩余列读取 |
| 对象存储 I/O | Sparse Range Coalesce、Parquet Active/Lazy 自适应合并、Data Cache | MergeRangeFileReader、Segment Prefetch、File Cache | Pruning 并发控制、Meta Cache、Projection/RowSelection |

三者架构风格不同，但核心模式高度一致：

```text
便宜且粗粒度的元数据
        ↓
更贵且细粒度的索引
        ↓
先读窄谓词列
        ↓
行级 Runtime/Residual Filter
        ↓
按 Selection 读取宽输出列
```

真正的差异主要在三个位置：元数据组织粒度、动态谓词能转成哪些可索引表示，以及稀疏读取与合并读取之间的成本模型。

## 进一步降低 Scan I/O 的三个方向

### 数据与布局：Projection、嵌套裁剪和聚簇

列式存储最便宜的字节是从未进入 Projection 的字节。除了普通列裁剪，还应把 JSON、Struct、Map、Array 的 Access Path 下推到子字段，避免为了 `payload.user.id` 读取完整 `payload`。这对日志和半结构化宽表往往比增加一个索引更稳定。

**数据布局、Compaction 与 Recluster。**

Zone Map 的效果来自值与物理位置的相关性。写入乱序、频繁小批量导入和长期不做 Compaction，会让重叠范围越来越宽。应结合查询 Predicate 频率选择 Sort/Cluster Key，并通过 Compaction 或 Recluster 恢复局部有序性。

这是一种典型的读写权衡：更强聚簇降低读取放大，但增加写放大、后台计算与数据重写成本。

### 索引与缓存：避免数据读取，也避免重复读取元数据

- Dictionary 让字符串谓词在小字典上计算一次，再过滤整数码；
- Bitmap 适合低到中等基数且组合过滤频繁的列；
- Inverted Index 适合文本、关键词、JSON Path 或高选择率点查；
- N-gram Bloom 可以为部分 `LIKE` 模式提供保守裁剪。

这些结构可能减少数据 Page I/O，也可能只是减少解码和表达式 CPU。评估时必须把索引读取字节、索引缓存命中和维护成本计入总账。

**Footer、Metadata 与 Data Cache。**

对象存储上的 Footer、Page Index 和 Bloom 本身也需要 I/O。缓存热点 Footer、Segment Meta、Block Meta 和索引页，能减少每次 Scan 的控制面请求；Data Cache 则把远端随机读取转成本地读取。

缓存没有减少逻辑读取量，却减少了远端字节和尾延迟。Profile 中应同时区分 `RemoteBytesRead`、`LocalCacheRead` 和 `CacheMiss`，否则容易把缓存收益误认为谓词下推收益。

### 请求调度：在少读字节与少发请求之间取舍

假设 Selection 需要读取同一对象中的三个范围：

```text
[0, 64KB]   [68KB, 96KB]   [104KB, 160KB]
```

严格稀疏读取发出三个请求；Coalescing 可以把它们合成一个 `[0, 160KB]` 请求，代价是多读间隙。对象存储中单请求延迟和计费可能比额外几十 KB 更重要，因此应设置最大合并距离、Buffer 上限和并发数，并根据实际命中自适应调整。

Prefetch 同样不是越多越好：顺序 Scan 中能隐藏延迟，选择率很高时却可能在过滤结果出来前把无用 Payload 拉入缓存。

**谓词顺序与自适应旁路。**

多个谓词应综合考虑：

```text
价值 ≈ 被过滤的后续字节 /（索引读取 + 解码 + 谓词计算成本）
```

高选择率但计算昂贵的正则表达式不一定应该最先执行；低成本的数值比较可能先排除足够多的行。运行时采样、动态重排和低收益 Runtime Filter 旁路，能避免“下推成功但查询变慢”。

**文件与 Page 尺寸。**

粒度越小，Min/Max 和 Bloom 越精确，但 Footer/Index 越大、对象数量和请求越多；粒度越大，顺序吞吐更好，却增加过滤后的读取放大。文件大小、Row Group/Block 大小和 Page 大小必须结合对象存储延迟、典型 Projection 宽度和 Predicate 选择率共同设计。

## Runtime Bloom 下推的三个边界

### 正确性边界：假阳性可以接受，假阴性不可接受

Runtime Filter 是 Join Build 侧键集合的近似摘要。只有在该摘要覆盖了语义所需的完整 Build 集合，并且 Join 类型允许时，Probe 侧才能安全丢行。分区 Bloom、全局 Bloom、Broadcast/Shuffle 布局不能混用；Outer Join、Anti Join、Null-safe Equality 也需要单独处理。

Bloom 的假阳性只会让更多行进入 Join，不影响结果；任何假阴性都会产生错误结果。因此，类型转换、字符串 Collation、Decimal Scale、时区、Null 和 Hash 版本都必须成为过滤器协议的一部分。

### 时间边界：Late Arrival 只能影响尚未读取的数据

Runtime Filter 到达时，Scan 可能处于三种状态：

1. 尚未生成 Scan Range：可参与完整元数据裁剪；
2. 已生成范围但尚未读取：可继续收缩剩余 Range；
3. 对应 Page 已经发出异步请求：通常只能过滤读回的行或阻止后续列物化。

因此，Profile 中的 `RuntimeFilterRows` 很高，不代表远端 `BytesRead` 同比例下降。要验证 I/O 收益，必须同时检查过滤器到达时间、Pruned Blocks/Pages、Lazy Read Rows 和实际远端字节。

### 表示边界：持久化 Bloom 与 Runtime Bloom 不能默认比较

持久化 Bloom 与 Runtime Bloom 如果要直接做集合不相交判断，至少要保证：

- 相同的值规范化与类型编码；
- 相同的 Hash 算法、Seed 和 Hash 次数；
- 相同或可证明兼容的位图布局与长度；
- Null 语义一致；
- 分区范围与数据块粒度能够正确对应；
- 合并判定只产生保守结果。

否则，“位图看起来没有交集”不代表两个值集合没有交集。更通用的做法仍是把 Runtime Filter 附带的 MinMax/小 InList 用于元数据层，把 Bloom 用在真实 Probe Key 上。

### 选择率不是唯一变量：用字节和时间计算收益

数据库 Profile 常把过滤率写成 `rows_before / rows_after`，但 Scan I/O 的收益更接近下面这个分解：

```text
SavedBytes
  ≈ PrunedPhysicalUnitsBytes
   + SurvivingRowsAvoidedPayloadBytes
   - ExtraIndexBytes
   - CoalescingOverReadBytes

NetBenefit
  ≈ SavedRemoteTime
   + SavedDecodeCPU
   - FilterWaitTime
   - HashAndPredicateCPU
   - ExtraRequestLatency
```

同样过滤 90% 的行，窄投影查询可能几乎省不下字节，宽 Payload 查询却可能收益巨大；同样少读 100 MiB，本地 NVMe 与高延迟对象存储的收益也不同。优化器或 Scan Scheduler 因此至少需要估计四类量：过滤器到达时间、列宽、物理聚簇程度以及请求合并后的实际 Range。

这也给出了一个更严格的实验方法：保持 SQL 和数据不变，分别关闭元数据裁剪、Runtime Filter、Late Materialization 与 I/O Coalescing，观察 Remote Bytes、Request Count、Decode CPU 和 Wall Time 的增量。只比较“优化全部打开”与“全部关闭”，无法判断收益来自哪一层，也无法发现某个子机制正在负优化。

## 可观测性：从 Query Profile 验证优化是否真的有效

调优时不要只看总耗时。建议按以下顺序建立证据链：

| 观察项 | 需要回答的问题 | 常见误区 |
|--------|----------------|----------|
| Planned/Total Blocks、Row Groups、Pages | 元数据裁剪跳过了多少物理单元？ | 只看返回行数，不看扫描单元 |
| Index/Meta Read Bytes 与时间 | 为跳读付出了多少索引成本？ | 忽略远端 Bloom/Page Index 请求 |
| Remote Requests 与 Remote Bytes | 是请求过多还是读取放大？ | 只优化字节，制造大量小请求 |
| Cache Hit/Miss | 远端收益是否只是缓存命中？ | 把 Cache 与 Predicate 收益混为一谈 |
| Predicate/Runtime Filter Rows | 哪个条件真正有选择率？ | 低收益过滤器持续消耗 CPU |
| Runtime Filter Build/Wait/Arrival | 过滤器是否赶在 Scan 前到达？ | 只看 Filter Rows，不看到达时机 |
| Lazy/Prewhere Rows 与 Bytes | 是否避免了 Payload 列读取？ | 过滤了行，但宽列已提前读取 |
| Decompress/Decode/Scan CPU | 瓶颈是否已经从 I/O 转到 CPU？ | 远端字节下降却总耗时不变 |

一个可靠的 A/B 实验至少应固定数据快照、并发、缓存冷热状态和文件布局，分别关闭 Page Index、Bloom、Runtime Filter 或 Late Materialization，并比较“远端请求 + 远端字节 + Scan CPU + 总耗时”。单次热缓存结果很容易给出错误结论。

## 研究脉络与延伸阅读

**轻量级块摘要。**

[Small Materialized Aggregates: A Light Weight Index Structure for Data Warehousing](https://www.vldb.org/conf/1998/p476.pdf) 在 VLDB 1998 中讨论了为连续物理 Bucket 保存小型聚合摘要。今天常见的 Min/Max、Count、Null Count 等 Zone Map，可以放在这一类思想下理解：摘要很小、顺序组织、易于批量生成，但效果高度依赖 Bucket 粒度与数据布局。

**延迟物化与位置表示。**

[Materialization Strategies in a Column-Oriented DBMS](https://www.cs.umd.edu/~abadi/papers/abadiicde2007.pdf) 系统比较了早期与延迟物化，并讨论用 Position List 连接列的收益和代价。三套引擎今天使用的 RowId、Selection、Bitmap、Active/Lazy Column，本质上都在工程化这一思想。

值得继续研究的不是“是否采用 Late Materialization”，而是：何时切换、位置集合用 Range/Bitmap/List 中哪种表示、何时合并远端请求，以及如何把真实选择率反馈给列读取顺序。

**Sideways Information Passing。**

[Sideways Information Passing for Push-Style Query Processing](https://www.cis.upenn.edu/~zives/research/push.pdf) 把 Bloom Join、Semijoin 和 Hash Filter 放入更一般的信息旁路框架：一个子表达式完成后，把集合摘要传给计划中其他相关位置，提前丢弃不可能产生结果的数据。现代 Runtime Filter 就是这条思路在 MPP Pipeline、分区 Hash Join 和异步 Scan 中的延伸。

今天的新问题主要来自分布式执行：过滤器布局如何匹配 Shuffle、何时等待、如何渐进合并、怎样处理晚到过滤器，以及过滤收益是否足以覆盖 Build、网络广播和 Probe 成本。

**工作负载感知的数据布局。**

[Learning Multi-Dimensional Indexes](https://arxiv.org/abs/1912.01668) 提出的 Flood 根据数据分布与过滤工作负载共同优化多维布局。它提醒我们：单一 Sort Key 很难同时服务多个相关维度，Z-order 或固定层次排序也不是所有数据集上的最优解。

对云数据仓库更现实的借鉴方向包括：根据历史 Predicate 自动选择 Cluster Key、评估 Recluster 收益、按工作负载调整 Page/Block 粒度，以及为相关列构建轻量级多维摘要。代价同样必须包含数据重写、索引维护和工作负载漂移。

**文件格式正在把跳读能力标准化。**

[Parquet Page Index](https://parquet.apache.org/docs/file-format/pageindex/) 将 ColumnIndex 与 OffsetIndex 分开：前者回答 Page 的值域，后者定位 Page 的物理位置；[Parquet Bloom Filter 规范](https://github.com/apache/parquet-format/blob/master/BloomFilter.md) 则定义了列块级存在性摘要。[ORC Specification](https://orc.apache.org/specification/ORCv1/) 也在 Stripe、Row Index 和 Bloom 等层级保存统计信息。

开源引擎未来的差异不会只在“是否支持这些格式特性”，而在是否能把静态谓词、Runtime Filter、删除向量、延迟物化和对象存储 Range Read 统一进一个成本可控的候选范围模型。

## 总结

如果目标是降低存算分离场景的 Scan 成本，可以按以下顺序推进：

1. **先验证 Projection。** 宽表中多读一个大字段，常常足以抵消其他裁剪收益。
2. **再验证数据布局。** 看过滤列的 Block/Page MinMax 是否真正收敛，而不是只确认“索引存在”。
3. **区分静态 Predicate 与 Runtime Filter。** 前者在 Scan 启动前可用；后者必须考虑 Build、合并、网络和等待。
4. **让 Runtime Filter 附带多种表示。** 小集合用 InList，中等集合用 Bloom，范围摘要用 MinMax；不同表示服务不同层级。
5. **把延迟物化收益落实到字节。** 观察 Payload Bytes 是否下降，而不只是 Filtered Rows 上升。
6. **给索引和 Runtime Filter 设计旁路。** 低选择率或高读取成本时，应停止使用收益不足的过滤器。
7. **联合优化 Range 与请求。** 同时约束稀疏程度、合并距离、单请求大小、Prefetch 深度和并发。
8. **把指标按层级归因。** 分开统计 Segment/Row Group/Page/Row、Static/Runtime、Meta/Data、Local/Remote。


谓词下推的终点不是“表达式进入了 ScanNode”，而是让越来越少的物理数据进入计算节点。要做到这一点，需要一条连续链路：

```text
数据布局提高元数据区分度
  → 粗粒度 Statistics 排除文件或块
  → Bloom/Bitmap/Inverted Index 缩小 Page 或 RowId
  → Runtime MinMax/InList 裁剪尚未读取的范围
  → Runtime Bloom 与静态谓词先过滤窄列
  → 延迟物化只读取存活行的宽列
  → I/O Coalescing、Cache 与 Prefetch 控制远端请求成本
```

StarRocks、Doris 与 Databend 的源码都印证了这一点。它们没有依赖某一个“万能索引”，而是把不同粒度、不同成本和不同到达时间的过滤信息逐层组合。最值得关注的也不是 Feature 名称，而是三个边界：**过滤发生在读取前还是读取后、节省的是远端字节还是仅仅 CPU、为裁剪付出的索引与请求成本是否小于收益。**

理解这三个边界，才能把“谓词已经下推”转化为可观测、可解释、可持续优化的 Scan I/O 收益。

## 关键源码阅读索引

| 主题 | 项目 | 源码入口 |
|------|------|----------|
| Native 多级裁剪 | StarRocks | [`segment_iterator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp) |
| Runtime Range Pruning | StarRocks | [`runtime_range_pruner.hpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_range_pruner.hpp) |
| Runtime Filter 行级执行 | StarRocks | [`runtime_filter_predicate.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_filter_predicate.cpp) |
| Parquet 元数据裁剪 | StarRocks | [`predicate_filter_evaluator.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/predicate_filter_evaluator.h) |
| Native 多级裁剪 | Apache Doris | [`segment_iterator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/segment_iterator.cpp) |
| Runtime Filter 表达式生成 | Apache Doris | [`runtime_filter_consumer.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/runtime_filter/runtime_filter_consumer.cpp) |
| Runtime Filter 存储谓词归一化 | Apache Doris | [`scan_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/scan_operator.cpp) |
| Native Page Bloom | Apache Doris | [`column_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/column_reader.cpp) |
| Parquet 裁剪与 Range Read | Apache Doris | [`vparquet_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/format/parquet/vparquet_reader.cpp) |
| Fuse Pruning | Databend | [`fuse_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/fuse_pruner.rs) |
| Runtime Block Pruning | Databend | [`expr_runtime_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/expr_runtime_pruner.rs) |
| Prewhere 与 Runtime Bloom | Databend | [`read_state.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/read_state.rs) |
| Runtime Filter 等待策略 | Databend | [`runtime_filter_wait.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/runtime_filter_wait.rs) |

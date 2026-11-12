---
title: "【源码】深入表达式下推：StarRocks、Doris 与 Databend 如何减少 Scan I/O"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从存算分离架构下一次对象存储读取的真实账单出发，深入对比 StarRocks、Apache Doris 与 Databend 的多级索引裁剪、Runtime Bloom 延迟物化及远端 Range 请求合并实现。"
draft: false
toc: true
math: true
---

在存算一体时代，本地 NVMe SSD 多读几十 KB 数据不过是微秒级的带宽抖动；可一旦进入存算分离（Storage–Compute Disaggregation）架构，计算节点（CN）与底层对象存储（S3 / OSS）隔着高延迟网络，一次草率的无效读取就会引发连锁反应：动辄几十毫秒的 HTTP `GET` 首包延迟、按请求次数计费的云账单、跨机房网卡带宽争抢、本地缓存（Data Cache）热页被无用数据挤出，外加解压与列式解码烧掉的 CPU 周期。

因此，当你在 `EXPLAIN` 计划里看到 `PREDICATES: ...` 挂在了 `ScanNode` 上时，千万别以为万事大吉。“谓词下推（Predicate Pushdown）”绝不是一个非黑即白的布尔开关。不妨在白板上摆一张典型的日志宽表——包含 `ts DATETIME`（按时间近似聚簇）、`user_id BIGINT`（8 字节随机分布）以及 `payload_json VARCHAR`（平均 2 KB 的大宽列），一共 100 万行数据，执行如下查询：

```sql
SELECT user_id, payload_json
FROM app_events
WHERE ts >= '2026-08-24 10:00:00'
  AND user_id = 42;
```

如果我们用最朴素的方式实现扫描算子，先把投影所需的 `ts`、`user_id`、`payload_json` 全量从对象存储拉回内存再逐行求值，光拉取 100 万行 `payload_json` 就要搬运近 **2 GB** 远端数据！为了把这笔冤枉钱省下来，现代列式存储引擎在读取路径上设下了层层递进的关卡：

1. **第一关（粗粒度范围裁剪）**：由于数据按 `ts` 有序或近似聚簇，绝大多数历史数据块/页的最小最大值区间（Zone Map / Page Index，如 `[08:00, 08:59]`）与 `>= 10:00` 毫无交集，引擎只看几十字节的尾部元数据就能整片跳过 **95%** 的物理页，**数据页 I/O 为零**；
2. **第二关（细粒度存在性裁剪）**：幸存下来的 5% 最新数据页里，`user_id` 随机散落，每页的区间都是 `[1, 999999]`，Zone Map 束手无策；此时引擎按需拉取页级持久化 **Bloom Filter**，发现绝大多数页的位图断言“`42` 必不存在”，再次免读数据页；
3. **第三关（窄列先读与两阶段延迟物化）**：倘若过滤条件不是静态常量 `42`，而是上层 Hash Join 在运行期动态传下来的 **Runtime Bloom Filter** 呢？文件里的持久化 Bloom 和内存里的运行时 Bloom 哈希种子、位图布局互不兼容，根本没法直接做位运算求交！引擎被迫老老实实掏钱把 8 字节的窄列 `user_id` 读进内存跑探测，但它绝不会傻到同时去读 2 KB 的 `payload_json`——而是先把命中的行号记成稀疏的 `RowId / Selection` 集合，最后**只为存活下来的寥寥几行去按需读取宽列页**；
4. **第四关（远端 I/O 请求合并）**：最后一步还藏着云存储特有的物理陷阱——假设存活行散落在同一个对象的 `[0, 64KB]`、`[68KB, 96KB]`、`[104KB, 160KB]` 三处，老老实实发三次稀疏 Range 请求，花在三次网络 RTT 上的时间远比多读中间那两段 4KB / 8KB 缝隙要贵得多！于是底层 I/O 调度器又会反过来把相邻小区间**合并（Coalescing）**成一次 `[0, 160KB]` 的整块拉取。

![谓词下推的生效层级：I/O 在哪一级被真正省掉](/images/predicate-pushdown-levels.svg)

*图 1：同一条谓词在扫描算子内部的五级下沉漏斗——前三层（文件/Segment → Block/RowGroup → Page）决定“物理页读不读”，后两层（行位置延迟物化 → 行值向量化过滤）决定“宽列读不读、后续算子算不算”。*

看清这四道关卡的成本账，我们就能抛开营销概念，深入 StarRocks、Apache Doris 与 Databend 的存储执行层源码（刻意聚焦在分区与分桶裁剪之后的单文件 `Segment / Parquet` 内部），逐一拆解多级索引求交、Runtime Bloom 边界以及云端 I/O 调度的真实工程实现。

---

## 一、统一物理模型：从 SQL 标量树到保守候选集与延迟物化

无论上层 SQL 写得多么花哨，一条谓词想在存储层省下真实 I/O，都必须先把标量表达式树（Predicate Tree）翻译成物理存储能听懂的“位置语言”。

在列存引擎内部，索引裁剪遵循一条铁律：**允许假阳性（误报），绝不允许假阴性（漏报）。** 假设某个数据页在列 `x` 上的 Zone Map 记录为 `min = 100, max = 199`：面对 `x >= 500`，引擎可以十拿九稳地证明整页为 `MustFalse` 直接跳过；面对 `x = 150`，索引只能保守回答“可能存在”，把该页留进候选集，等读出真实列值后再用残余谓词（Residual Predicate）做精确复核。

这套“保守上界收敛”决定了不同索引的天然分工：

- **有序键与区间摘要（Short Key / Zone Map / Parquet PageIndex）**：体积袖珍、判定极快，但战斗力完全仰仗**物理聚簇度（Clustering）**。一旦高频小批量乱序写入且后台 Compaction / Recluster 没跟上，所有页的 `[min, max]` 彼此大面积重叠，区间裁剪就会退化成摆设。
- **持久化存在性摘要（Page / Block Bloom Filter）**：不关心数据是否有序，专治高基数等值点查（`=` 与短 `IN` 列表），返回 `false` 即可安全跳页；但它自身需要占用额外存储并消耗一次索引页读取与哈希计算，若前序区间索引已经把范围缩得极窄，再去拉取 Bloom 索引反而得不偿失。
- **两阶段延迟物化（Late Materialization）**：当所有读前索引都拦不住时，把列拆成“先读过滤列（Predicate / Join-Key Columns）”与“后读输出宽列（Payload / Lazy Columns）”两批。它的净收益取决于 `被省下的宽列字节 × 过滤淘汰率` 能否盖过二次随机定位（Seek）、位置位图维护与列块拼装的额外开销——一旦过滤率很低（比如 90% 的行都存活）或者表本身只有几个定长窄列，拆成两阶段反而比一次性顺序扫完更慢。

下面我们就顺着这套统一的收益漏斗，横向审视三套引擎是如何在代码里把“读前索引裁剪 → 窄列过滤 → 宽列按需拉取 → 远端请求合并”拧成一股绳的：

![StarRocks、Doris 与 Databend 存储层多级裁剪与两阶段延迟物化](/images/diagrams/aug-opt-pushdown-scan-engines.svg)

*图 2：StarRocks（`SparseRange`）、Doris（Roaring `_row_bitmap`）与 Databend（`PartInfo` + `RowSelection`）在存储读取器内的三段式协同——先以多级读前索引收敛候选行集，再以窄列过滤驱动宽列延迟读取，最后在对象存储边界合并碎片区间。*

---

## 二、StarRocks：以 `SparseRange` 为中枢的九级求交与双通道 Runtime Filter

打开 StarRocks BE 的原生列存读取器 [`segment_iterator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp)，源码在索引初始化序列上方留了一句醒目的警告注释：`/// the calling order matters, do not change unless you know why.`

在新近主干（`f977b53`）中，原本庞大的 `_init_internal()` 被拆解为一次性上下文初始化与可复用的扫描范围构造函数 `_init_scan_range_and_context()`。整套读前过滤像流水线一样，把不同粒度的索引命中结果统一收敛进同一个数据结构——**稀疏行号区间 `SparseRange`（`_scan_range`）**：

1. 从完整 Segment 行号区间出发，先按分片范围与有序短键索引（`Short Key / Tablet Range`）做二分查找，卡住首尾物理行号；
2. 应用主键表或明细表的删除掩码（`Delete Vector`，部分场景可延后）与低基数位图索引（`Bitmap Index`）；
3. 依次跑页级区间裁剪（`Zone Map`）、持久化布隆过滤器（`_get_row_ranges_by_bloom_filter()`）以及全文/倒排索引（`Inverted Index`）；
4. 最后衔接删除条件与向量检索（ANN Index）或采样逻辑，吐出最终缩水后的 `SparseRange`（主干还专门引入了 `SegmentReadStateCache`，允许拆分子迭代器时通过 `_init_reused_scan()` 直接复用已算好的页级 Zone Map / Bloom 裁剪结果，免去重复读索引页的开销；同时精细区分了不可重入的数据采样与必须重建距离映射表的向量索引）。

每走完一级索引，新的命中区间就与当前 `_scan_range` 原地求交。这意味着排在后面的昂贵索引（比如需要额外读盘算哈希的持久化 Page Bloom）**只需要检查前序便宜索引幸存下来的那几个残余 Page**，并且底层列读取器根本不用关心行号是被哪种索引裁掉的，只管照着最终的 `SparseRange` 跳页解码。

更精妙的设计在 **Runtime Filter 下沉到存储层时的“双通道分流”**。很多人以为 Join 传下来的 Runtime Filter 是一坨铁板一块的黑盒，StarRocks 却把它劈成了两条截然不同的物理通道：

![Runtime Filter 双通道下推：元数据区间裁剪 vs 行级 Bloom 延迟物化](/images/diagrams/aug-opt-pushdown-runtime-bloom.svg)

*图 3：Runtime Filter 进入扫描层的双通道分流——`MinMax / IN` 化身为 `index_filter_only` 谓词直接查询 Zone Map 整页免读；而无法与持久化 Bloom 直交的 `Runtime Bloom` 则先读窄连接键列压缩 `RowId`，靠延迟物化省掉宽输出列 I/O。*

- **通道 A（读前元数据区间裁剪，[`runtime_range_pruner.hpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_range_pruner.hpp)）**：从已到达的运行时过滤器里抽取出 `MinMaxFilter` 与小型 `InFilter`（主干已迁至 `be/src/compute_env/`，并支持对 `slot + c`、`slot - c` 等单调表达式做带 `__builtin_sub_overflow` 溢出保护的反向区间求解），转换成打着 **`index_filter_only` 钢印**的存储层 `ColumnPredicate` 去查页级 Zone Map！为什么要打 `index_filter_only` 标记？设想 Build 端只有 `{1, 100}` 两个值，抽出的区间是 `[1, 100]`——拿它去把 `[200, 300]` 的数据页整页踢掉绝对安全，但绝不能误以为落在 `[1, 100]` 里的 `50` 真的匹配成功了！即便过滤器在扫描中途才姗姗来迟，`_try_to_update_ranges_by_runtime_filter()` 也会在页边界动态拦截，把尚未读取的后半截 `_scan_range` 当场削窄。
- **通道 B（行级探测联动延迟物化，[`runtime_filter_predicate.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_filter_predicate.cpp)）**：那重头戏 **Runtime Bloom Filter** 呢？在 `segment_iterator.cpp` 里至今留着一行诚实的注释：`TODO: support runtime bloom filter push down to page level`。因为写文件时固化的持久化 Page Bloom 与引擎内存里的 Runtime Bloom 分属两套哈希与位图协议，没法隔空判不相交。但 StarRocks 把它无缝焊进了延迟物化管线：`_build_column_oriented_rf()` 把 Runtime Bloom 绑定到对应的连接键列上，先调 `_predicate_evaluate_late_materialize_read_first_column()` 读出窄小的连接键列（若是全字典编码列，甚至直接在字典词表上跑一遍过滤再映射回字典码！），算完 Runtime Bloom 大幅压缩存活 `RowId` 后，最后才调 `decode_values_by_rowid()` 拉取宽大的输出列。同时引擎会实时采样每个过滤器的淘汰率——最多保留 3 个有效过滤器，一旦发现某条过滤器独挡一面（淘汰率 \(\ge 95\%\)）就只跑这一个，若是几乎滤不掉行则果断旁路止损。

换到外部数据湖的 **Parquet 读取路径**（[`predicate_filter_evaluator.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/predicate_filter_evaluator.h) 与 [`group_reader.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/group_reader.cpp)），同样的思想换了一身贴合开放格式的行头：先串行过一遍 Row Group 统计信息、Parquet `PageIndex`（`ColumnIndex + OffsetIndex`）与 Row Group Bloom；进组后把列切成 `Active`（谓词列）与 `Lazy`（延迟输出列），`Active` 列一旦把当前批次筛成空集就立刻跳过整批 `Lazy` 列读取。更接地气的是参数 `io_coalesce_adaptive_lazy_active`——它会观察前几批里 `Lazy` 列到底有没有被真正跳过：如果发现查询过滤率很低、`Lazy` 列反正每次都要读，干脆把 `Active` 和 `Lazy` 的远端区间合并成一个大请求一口气拉回来，省去二次访问对象存储的网络往返！

---

## 三、Apache Doris：Roaring `_row_bitmap` 交集与嵌套子路径延迟读取

走进 Apache Doris BE 的 [`segment_iterator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/segment_iterator.cpp)，你会看到一位与 `SparseRange` 对偶的老朋友——**压缩位图 `Roaring Bitmap`（`_row_bitmap`）**。

在 `_lazy_init()` 阶段，Doris 先把整个 Segment 的全量行号塞进 `_row_bitmap`，随后按成本与选择率动态编排读前裁剪：先走有序短键范围 `_get_row_ranges_by_keys()`（针对 Z-order 或带 Cluster Key 的表自动跳过前缀假设），再结合列条件走倒排索引（Inverted Index）、字典、页级 Bloom 与 Zone Map。值得一提的是新近主干（`7ac0eb2f`）加入的**候选集反向喂给倒排索引优化**：当其他轻量限制条件已经先把 `_row_bitmap` 削掉一大截、且倒排索引支持消费候选集（`candidate_rows` 比例落在合理区间）时，引擎会先应用范围限制再查倒排索引；同样地，[`column_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/column_reader.cpp) 中的 `get_row_ranges_by_bloom_filter()` 也会先查 `Ordinal Index` 找出当前 `_row_bitmap` 覆盖的幸存页，绝不为早已被淘汰的数据页白跑一趟持久化 Bloom I/O。

当上层 Join 的 Runtime Filter 抵达扫描算子时（默认远端等待窗口 `runtimeFilterWaitTimeMs = 1000` 毫秒，超时即先行开扫，晚到的过滤器只作用于剩余未读分片），[`runtime_filter_consumer.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/runtime_filter/runtime_filter_consumer.cpp) 与 [`scan_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/scan_operator.cpp) 的 `_normalize_predicate()` 会剥开包装壳，按过滤器物理形态精准分流：

- 运行时 `IN` 与 `MinMax` 被归一化为 `ColumnValueRange` 及存储层谓词，直接杀进读前阶段参与 Short Key、Zone Map 以及 Parquet `RowGroup / PageIndex` 跳页；
- 运行时 `Bloom` 则化身为 [`BloomFilterColumnPredicate`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/predicate/bloom_filter_predicate.h)，在解码出的 `IColumn`（支持字典码直筛与低效采样熔断）上做向量化求值。

在延迟物化编排上，Doris 的 `_vec_init_lazy_materialization()` 把列划分得比传统二分法更细，切成了三级梯队：

1. **`_predicate_ordinals`**：第一顺位读取，跑完所有能够用列原语加速的静态谓词与 Runtime Bloom；
2. **`_common_expr_ordinals`**：紧随其后，仅为第一轮幸存行读取复杂公共标量表达式所需的输入列；
3. **`_output_ordinals`**：最后通过 `_read_columns_by_rowids()` 精准拉取最终存活行的纯粹投影宽列（针对 `Struct / Array / Map` 半结构化列，甚至能把谓词引用的深层子列路径与最终输出的其他子路径拆开，过滤完再按需补齐嵌套字段！）。

而在外部湖表侧，[`vparquet_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/format/parquet/vparquet_reader.cpp) 与 `vparquet_group_reader.cpp` 同样贯通了“Row Group 统计/Bloom → `ColumnIndex/OffsetIndex` 页级 `candidate_row_ranges`（并入 Iceberg/Delta 删除向量）→ 先读谓词列算 `FilterMap` → Lazy 列跳读”全链路，并在物理字节层由 `MergeRangeFileReader` 把细碎的随机访问区间聚合成大块远端请求。

---

## 四、Databend：完全云原生的 Fuse Block Pruning 与自适应 `Prewhere`

与从本地 SSD 演进到存算分离的 StarRocks、Doris 不同，用 Rust 编写的 Databend 从第一天起就长在对象存储之上，并且在新近演进（PR `#19982`，提交 `9431970`）中大刀阔斧地移除了早期自研的 `Native` 存储格式，全面收敛到基于开放 **Parquet** 格式的 Fuse 引擎路径上。

面对对象存储动辄数十毫秒的元数据读取延迟，Databend 在扫描启动前专门辟出了一个受信号量（Semaphore）严格限流的异步并行裁剪阶段——[`fuse_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/fuse_pruner.rs)：

- 沿着 `Segment Pruner → Block Pruner → TopN Pruner` 三级流水线，利用本地缓存的 `Block Meta` 统计信息做值域推导（Domain Folding），一旦证明 `MustFalse` 或不可能挤进 `ORDER BY ... LIMIT N` 的前 N 名，连整个 Parquet 数据块（Block）的边都不碰；
- 面对等值或短 `InList` 点查，按需拉取独立的块级持久化 Bloom Index 文件做存在性否决，同时靠并发度门禁防止为了读索引反而把 S3 请求打爆。

当 Hash Join 构建端（[`local_builder.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/service/src/pipelines/processors/transforms/hash_join/runtime_filter/local_builder.rs)）发来运行时过滤器、且扫描端在 [`runtime_filter_wait.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/runtime_filter_wait.rs)（50ms 轮询、监听取消信号）等到位后，Databend 把不同过滤器形态的职责切分得如同教科书般工整：

- **`MinMax` 与小型 `InList` 走读前裁剪（[`expr_runtime_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/expr_runtime_pruner.rs)）**：直接与 Block 统计区间做折叠求值跳过整块；更妙的是，当运行时 `InList` 元素个数不超过阈值时，它还会顺手去查持久化 Block Bloom Index——相当于架起了一座从“运行时连接键值”直通“静态文件索引”的桥梁！
- **`Runtime Bloom` 汇入 `Prewhere` 两阶段读取（[`read_state.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/read_state.rs) 与 [`parquet_data_source_deserializer.rs`](https://github.com/databendlabs/databend/blob/17de8708fd4095e6a062b6100789b9bc351a7230/src/query/storages/fuse/src/operations/read/parquet_data_source_deserializer.rs)）**：引擎把静态 `Prewhere` 过滤列与 `Runtime Bloom` 探测列合并成第一批预读投影（`Preread Projection`），算完位图交集后当场评估选择率——**只有当淘汰率足够高时，才把位图转成 `RowSelection`（配合默认开启的 `enable_parquet_page_index`）跳读剩余宽列；一旦发现淘汰率平平，立刻退回整块顺序反序列化再内存过滤**，彻底杜绝了“为了少读三五行数据，把顺序大 I/O 切碎成几百个稀疏小 Seek”的反向负优化。

---

## 五、横向能力矩阵与 Scan I/O 的真实盈亏方程

把 StarRocks、Doris 和 Databend 放在一起对照，会发现尽管三者的候选集数据结构（`SparseRange` vs `Roaring Bitmap` vs `PartInfo + RowSelection`）各有千秋，底层遵循的物理经济学却高度统一：

| 对比维度 | StarRocks | Apache Doris | Databend |
| --- | --- | --- | --- |
| **读前候选行集载体** | 稀疏区间 `SparseRange`（含 `SegmentReadStateCache` 复用） | 压缩位图 `Roaring _row_bitmap` | 块级 `PartInfo` + Parquet `RowSelection` |
| **有序键与区间索引** | Short Key + Segment/Page Zone Map；Parquet PageIndex | Short Key + Zone Map；Parquet Statistics/PageIndex | Cluster Key 聚簇 + Block Stats + Parquet PageIndex |
| **持久化存在性索引** | Native Page Bloom；Parquet Row Group Bloom | Ordinal 定位幸存页后按需查 Page Bloom | 独立异步限流 Block Bloom Index |
| **Runtime MinMax / IN** | 转 `index_filter_only` 谓词动态收缩未读 Zone Map | 归一化为 `ColumnValueRange` 复用短键与区间索引 | Block 统计域折叠；短 `InList` 额外直查持久化 Bloom |
| **Runtime Bloom 生效点** | 先读 Probe 窄列（或字典词）筛 `RowId`，驱动延迟物化 | `BloomFilterColumnPredicate` 列值过滤，驱动三级延迟读取 | 并入 `Preread / Prewhere` 窄列，高淘汰率下推 `RowSelection` |
| **对象存储请求优化** | `SparseRange` 合并、`io_coalesce_adaptive_lazy_active`、Data Cache | `MergeRangeFileReader`、Segment 预取、File Cache | 信号量限流裁剪、Meta Cache、低选择率退回顺序整读 |

这也解释了为什么在线上调优时，绝不能单凭 Query Profile 里一句漂亮的 `RowsFiltered = 90%` 就断定扫描变快了。如果我们把存算分离下一次 Scan 的真实字节账单与墙钟耗时账单写成公式，所有隐性成本便无处遁形：

$$\text{SavedBytes} \approx \text{PrunedPagesBytes} + \text{LazySkippedPayloadBytes} - \text{ExtraIndexBytes} - \text{CoalesceGapBytes}$$

$$\text{NetTimeSaved} \approx \text{SavedRemoteTransfer} + \text{SavedDecodeCPU} - \text{RFWaitTime} - \text{BloomHashCPU} - \text{ExtraSeekRTT}$$

顺着这两个等式看，生产环境里最常见的四类“下推了反而没快（甚至变慢）”瞬间就能对号入座：

1. **窄投影陷阱**：查询只写了 `SELECT count(*) FROM t JOIN d ON t.k = d.k`，投影里除了连接键 `t.k` 没有任何宽 Payload 列。此时 Runtime Bloom 既跳不过持久化数据页，也没有宽列可供延迟物化省流，省下的 I/O 字节严格为 **0**，还要倒贴全表算哈希的 CPU（唯一收益只剩减少送往上层算子的行数）。
2. **迟到失效（Late Arrival）**：Build 端建表太慢，超过了扫描端的等待窗口（如 1000ms），等过滤器跨网送到时，异步预取器早就把 80% 的数据页拉进内存了——Profile 里 `RuntimeFilterRows` 可能依然很高，但 `RemoteBytesRead` 纹丝不动。
3. **聚簇崩坏**：写入端高频小批次乱序灌数且 Compaction 积压，每个 Page 的 `min..max` 都横跨全月，Zone Map 与 Runtime MinMax 命中率归零；或者反过来，为了极致的微页跳读把 Page 切得太碎，导致 Footer / PageIndex 膨胀、S3 `GET` 请求数翻倍。
4. **缓存幻觉**：做 A/B 对比实验时第一遍跑热了本地 NVMe Data Cache，第二遍加了索引发现飞快——其实省掉的只是远端慢请求，如果不分清 `RemoteBytesRead`、`LocalCacheRead` 与 `IndexReadBytes`，极易把缓存命中的功劳错记在谓词下推头上。

---

## 六、经典论文脉络、线上 Profile 排障清单与源码索引

今天我们在三套开源内核里看到的精巧机关，其实是过去二十余年数据库系统界四条研究主线在云原生时代的工程合流：

- **轻量级块摘要（Zone Map / SMA）**：源自 VLDB 1998 的 [《Small Materialized Aggregates: A Light Weight Index Structure for Data Warehousing》](https://www.vldb.org/conf/1998/p476.pdf)，用极低维护成本的连续桶极值换取大块顺序跳读，直至演化出如今标准化的 [Parquet Page Index](https://parquet.apache.org/docs/file-format/pageindex/)（物理分离值域 `ColumnIndex` 与偏移 `OffsetIndex`）、[Parquet Bloom Filter](https://github.com/apache/parquet-format/blob/master/BloomFilter.md) 与 [ORC Stripe/Row Index](https://orc.apache.org/specification/ORCv1/)；
- **列存延迟物化（Late Materialization）**：Abadi 等人在 ICDE 2007 名作 [《Materialization Strategies in a Column-Oriented DBMS》](https://www.cs.umd.edu/~abadi/papers/abadiicde2007.pdf) 中奠定了用位置列表（Position List）解耦过滤列与输出列的代价模型；
- **横向信息旁路（Sideways Information Passing）**：Ives 等人在 [《Sideways Information Passing for Push-Style Query Processing》](https://www.cis.upenn.edu/~zives/research/push.pdf) 中提出的跨子树摘要传递，正是当代分布式 MPP Runtime Filter 的理论先声；
- **工作负载感知的多维聚簇**：如 [《Learning Multi-Dimensional Indexes (Flood)》](https://arxiv.org/abs/1912.01668) 所揭示的，单一排序键难以兼顾多维过滤，结合历史谓词频率做自动 Recluster 与半结构化 JSON/Variant 子列路径裁剪，正在成为云数仓压降扫描成本的新主战场。

回到日常研发与线上慢查诊断，当你怀疑某条慢 SQL 的扫描层做了无用功时，只需固定冷热缓存状态，按以下五步核对 Query Profile：

1. **看物理跳读比（`TotalPages` vs `PrunedPages / FilteredBlocks`）**：确认静态谓词与 Runtime MinMax/IN 是否真正挡住了物理单元，若几乎为零，优先排查排序键聚簇度与乱序未合并的 Rowset；
2. **算索引与合并税（`IndexReadBytes`、`RemoteRequests` vs `RemoteBytesRead`）**：平均单次远端请求若跌破几十 KB，说明稀疏跳读切得太碎，急需调优 Range Coalescing 合并阈值；
3. **盯过滤器时序（`RFWaitTime` 与到达时刻）**：确认 Runtime Filter 是赶在开扫前就位，还是扫完大半才迟到；
4. **验延迟物化实效（`LazyReadRows` 与宽列读取字节）**：核实高淘汰率条件是否排在了第一批谓词列执行，防止昂贵正则或低效过滤器抢跑；
5. **剥离缓存干扰（`LocalCacheHit` vs `CacheMiss`）**：在做任何参数或索引收益归因前，务必先看冷读下的真实远端字节减量。

最后附上三套引擎扫描裁剪与延迟物化的核心源码入口：

| 核心模块 | 所属引擎 | 关键源码路径 |
| --- | --- | --- |
| 原生 `SparseRange` 多级求交与延迟物化 | StarRocks | [`segment_iterator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment_iterator.cpp) |
| Runtime Filter 区间裁剪与行级采样执行 | StarRocks | [`runtime_range_pruner.hpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_range_pruner.hpp)、[`runtime_filter_predicate.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/runtime_filter_predicate.cpp) |
| Parquet 统计/页索引评估与 Active/Lazy 自适应合并 | StarRocks | [`predicate_filter_evaluator.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/predicate_filter_evaluator.h)、[`group_reader.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/formats/parquet/group_reader.cpp) |
| Roaring `_row_bitmap` 裁剪与三级延迟列读取 | Apache Doris | [`segment_iterator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/segment_iterator.cpp)、[`column_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/storage/segment/column_reader.cpp) |
| Runtime Filter 谓词归一化与 Parquet 合并读 | Apache Doris | [`runtime_filter_consumer.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/runtime_filter/runtime_filter_consumer.cpp)、[`scan_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/scan_operator.cpp)、[`vparquet_reader.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/format/parquet/vparquet_reader.cpp) |
| Fuse 异步限流块裁剪与运行时统计折叠 | Databend | [`fuse_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/fuse_pruner.rs)、[`expr_runtime_pruner.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/pruning/expr_runtime_pruner.rs) |
| Parquet `Prewhere` 自适应 `RowSelection` 与等待轮询 | Databend | [`read_state.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/read_state.rs)、[`parquet_data_source_deserializer.rs`](https://github.com/databendlabs/databend/blob/17de8708fd4095e6a062b6100789b9bc351a7230/src/query/storages/fuse/src/operations/read/parquet_data_source_deserializer.rs)、[`runtime_filter_wait.rs`](https://github.com/databendlabs/databend/blob/ab6f27c6aaa53d31f3417bb26b3ab970d7dd0456/src/query/storages/fuse/src/operations/read/runtime_filter_wait.rs) |

---

> **源码基线说明**：本文初稿基于 `StarRocks/starrocks` 提交 `0fd27fd`（2026-03-30）、`apache/doris` 提交 `5202d06`（2026-08-21）及 `databendlabs/databend` 提交 `ab6f27c`（2026-05-27），并于 2026-10 对照上游快照 StarRocks `f977b53`、Doris `7ac0eb2f`、Databend `17de870` 完成源码复核。两快照间关键演进——包括 Databend 在提交 `9431970`（PR `#19982`）中正式下线旧版 `Native` 存储格式并统一至 Parquet `ReadState + RowSelection` 路径、StarRocks 拆分 `_init_scan_range_and_context()` 引入 `SegmentReadStateCache` 并将 `runtime_range_pruner.hpp` 迁至 `be/src/compute_env/`、以及 Doris 为倒排索引支持 `candidate_rows` 候选位图下推——均已同步融入正文。

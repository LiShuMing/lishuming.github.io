---
title: "执行引擎深水区三题：COW Column 内存所有权、多列 Runtime Filter 与分布式精确 Count Distinct"
date: 2026-11-22T18:10:00+08:00
lastmod: 2026-11-22T18:10:00+08:00
slug: "cow-column-multicolumn-rf-and-exact-count-distinct"
categories:
  - 数据库
  - 大数据
tags:
  - 执行引擎
  - Copy-on-Write
  - Runtime Filter
  - Count Distinct
  - StarRocks
  - Apache Doris
  - ClickHouse
  - DuckDB
  - Velox
  - Trino
  - Snowflake
  - Databricks
  - BigQuery
description: "结合 StarRocks BE/FE 源码与生产缺陷复盘，并横向对照 ClickHouse、Doris、DuckDB、Velox、Trino、Snowflake、Databricks Photon、BigQuery 与学术界 Predicate Transfer，深入剖析执行引擎中 COW Column 内存所有权、多列全局 Runtime Filter 跨 Exchange 路由下推，以及大规模分布式精确 COUNT(DISTINCT) 的多阶段拓扑与抗倾斜设计。"
draft: false
toc: true
math: true
---

在一套 30 TB 级的分布式向量化 MPP 引擎上跑下面这条并不算罕见的分析查询：

```sql
SELECT
    l.country,
    COUNT(DISTINCT l.user_id)   AS uv,
    COUNT(DISTINCT l.device_id) AS dv,
    COUNT(map_keys(l.ext_tags)) AS tag_cnt
FROM fact_events l
JOIN dim_campaigns r
  ON l.campaign_id = r.campaign_id
 AND l.event_date  = r.event_date
WHERE r.channel = 'search'
GROUP BY l.country;
```

这条 SQL 只有二十几行，但在进入执行引擎的瞬间，它会同时撞上现代分析型数据库内核里最棘手的三堵墙：

1. **列内存所有权之墙**：`SELECT map_keys(l.ext_tags)` 既要往下游保留 `ext_tags`，又要构造出它的键数组列；随着 `Chunk` 在 `Scan → Project → Filter → Join → Aggregate` 之间高速流转，如果每次算子修改列都深拷贝整块内存，CPU 会把大量周期白白烧在 `memcpy` 上；如果放任裸指针或普通智能指针就地修改，又会在 CTE 共享、常量列展开或 `MapColumn` 子列复用处静默写坏兄弟列，酿成极难复现的线上崩溃。
2. **多列运行时过滤之墙**：`dim_campaigns` 过滤 `channel = 'search'` 后只剩极少数据，但连接键是双列 `(campaign_id, event_date)` 且走 Shuffle Join。每个 Join Build 实例手里只有按 `hash(campaign_id, event_date) % N` 路由到自己的局部键集合。想把运行期生成的过滤器推过 `ExchangeNode`、在扫表前把 99% 无效事实行掐死，Scan 端就必须同时掌握全部分区列的联合哈希路由；而在存储层延迟物化（Late Materialization）体系下，单列索引谓词又根本拿不到“尚未解码的另一列”。
3. **精确去重与倾斜之墙**：`COUNT(DISTINCT user_id)` 和 `COUNT(DISTINCT device_id)` 无法像 `SUM` 那样在各节点各自算完直接相加，必须在某个物理节点把相同的 `(country, user_id)` 汇聚到一起。一旦 `country = 'CN'` 占据 80% 流量，按 `country` 分区的三阶段聚合会立刻单点 OOM；而如果为了同时算两个 `COUNT(DISTINCT)` 粗暴地把每行复制两份（`Expand`），30 TB 表的 Shuffle 流量瞬间翻倍，五列去重就是五倍灾难。

表面上看，**Copy-on-Write Column（写时复制列）**、**多列 Runtime Filter（运行时过滤）**和**分布式精确 `COUNT(DISTINCT)`** 分属内存模型、连接剪枝与聚合规划三个模块；可退后一步看，它们共同回答着同一个第一性原理命题：**数据在多阶段算子与跨节点流水线中流转时，如何把不必要的数据拷贝、无效扫描和网络搬运压到极限，同时死守隔离性、路由正确性与倾斜鲁棒性三条底线？**

本文以 StarRocks FE/BE 真实源码与历史 PR（包括落地后的线上崩溃与默认回退复盘）为解剖切口，横向对照 ClickHouse、Apache Doris、DuckDB、Meta Velox、Apache Arrow/DataFusion、Trino，以及 Snowflake、Databricks Photon、BigQuery 和学术界最新研究（LIP、Predicate Transfer、RPT+），把这三道执行引擎深水区难题的设计空间彻底拆透。

---

## 一、执行引擎的内存所有权博弈：从 COW Column 源码到真实崩溃复盘

### 1.1 为什么非要在“深拷贝”和“原地写”之间走钢丝？

向量化引擎按批（`Chunk` / `Block` / `DataChunk` / `RecordBatch`，通常 1024–4096 行）在算子间传递列数组。一个最朴素的疑问是：既然担心写坏别人的数据，每次算子要修改一列（例如执行 `filter()` 压实存活行、给 `NullableColumn` 补齐空值位图、或把 `ConstColumn` 展开为普通列）之前，直接调用 `column->clone()` 拷贝一份不行吗？

StarRocks 源码里自带一份专门测量拷贝与 COW 开销的微基准 `be/src/bench/cow_bench.cpp`（104 核 3.2 GHz 环境，4096 行标准 Chunk 对比 `column->clone()` 深拷贝与 `Column::mutate(std::move(col))` 写时复制独占迁移）：

| 列类型与规模（单 Chunk 行数） | `column->clone()` 深拷贝 CPU 耗时 | `Column::mutate()` 独占复用 CPU 耗时 | 延迟差距 |
| :--- | ---: | ---: | ---: |
| `BIGINT`（4,096 行） | 2,028 ns | 302 ns | ~6.7× |
| `DECIMAL`（4,096 行） | 23,267 ns | 299 ns | ~77.8× |
| `VARCHAR(10)`（4,096 行） | 33,013 ns | 402 ns | ~82.1× |
| `VARCHAR(100)`（4,096 行） | 224,828 ns | 1,022 ns | **~220×** |
| `VARCHAR(1000)`（4,096 行） | 2,219,392 ns | 1,242 ns | **~1,787×** |
| `JSON`（4,096 行） | 47,770 ns | 480 ns | ~99.5× |
| `STRUCT<VARCHAR, UINT64>`（4,096 行） | 24,549 ns | 566 ns | ~43.4× |
| `VARCHAR(100)`（4,096,000 行大块） | 256,827,582 ns（256.8 ms） | 3,313 ns（3.3 µs） | **~77,521×** |

数据非常刺眼：定长整型拷贝尚且差一个数量级，一旦进入变长字符串、JSON 或嵌套 `STRUCT`/`MAP`，深拷贝就会从纳秒级跌进毫秒级，而且每次分配还会撞穿 L2/L3 缓存并给内存分配器制造剧烈抖动。

反过来，如果完全放弃隔离，让所有算子都拿着 `std::shared_ptr<Column>` 随意原地修改，又会在以下四种真实场景立刻炸掉：
- **Multicast CTE / Broadcast 分叉**：同一个 `Chunk` 同时发给下游两个 Pipeline 消费者，消费者 A 执行 `filter()` 原地改写数组长度，消费者 B 直接越界读崩溃；
- **表达式公共子列复用**：同一个 `Chunk` 里 `Project` 把上游列直通出去，同时又用它计算新列，或者 `map_keys(c2)` 零拷贝借用了 `MapColumn` 内部的子列；
- **常量列与字典列共享**：表达式缓存了常量 `ColumnPtr`，下游一旦原地修改就会污染后续所有批次；
- **哈希表/排序缓冲驻留**：`HashJoin` Build 侧或 `ChunksSorter` 长期持有输入列指针，上游如果复用缓冲原地覆盖就会静默算错数。

于是，最自然的中间解浮出水面：**Copy-on-Write（写时复制，COW）**——共享时永远只读，需要修改时先检查当前对象是否被独占（引用计数等于 1）：如果独占，直接零拷贝原地修改；如果被多方共享（引用计数大于 1），才付出代价深拷贝出一份独占副本。

### 1.2 解剖 StarRocks `Cow<Derived>`：编译期契约与运行期分流

StarRocks 在 `#56286`、`#56287`、`#56662` 等系列提交中，借鉴 ClickHouse 与 Rust 所有权思想，在 `be/src/common/cow.h` 引入了侵入式写时复制基类 `Cow<Derived>`，让 `class Column : public Cow<Column>` 从语言类型层面把“只读共享”和“独占可变”劈开。

![执行引擎内存所有权模型：COW Column 状态机、三大生产暗礁与竞品分层对照](/images/diagrams/exec-cow-column-ownership-and-traps.svg)

这套抽象由四个协作组件构成：

1. **侵入式原子引用计数 `RCPtr<T>` 与 `_use_count`**：计数直接嵌在 `Column` 对象头里（`std::atomic<uint32_t>`），省去 `std::shared_ptr` 独立控制块的二次堆分配与指针解引用开销。
2. **独占可变指针 `MutPtr<T>`（对外别名 `MutableColumnPtr`）**：**显式删除了拷贝构造函数**（`MutPtr(const MutPtr&) = delete;`），只允许 `std::move` 转移所有权。任何新 `create()` 出来的列都是 `MutablePtr`，编译器在编译期保证一份可变列不可能被偷偷复制出第二份引用。
3. **只读共享指针 `ImmutPtr<T>`（对外别名 `ColumnPtr`）**：底层持有 `const T*`，允许自由拷贝共享（递增 `_use_count`），但只能调用 `const` 成员函数。更关键的是，`ImmutPtr(const MutPtr<U>&) = delete;` 堵死了“一边留着可变句柄、一边复制出只读句柄”的漏洞——要想把可变列发布成共享只读列，必须通过 `std::move(mut_ptr)` 交出可变权。
4. **变色龙包装指针 `ChameleonPtr<T>`（对外别名 `WrappedPtr`）**：专门用于嵌套复合列的成员字段（如 `NullableColumn` 里的 `_data_column` / `_null_column`，`ArrayColumn` 里的 `_elements` / `_offsets`，`ConstColumn` 里的 `_data`）。它的巧妙之处在于利用 C++ `const` 重载实现“权限传染”：当外层 `NullableColumn` 是只读引用时，`WrappedPtr` 退化为只读 `ImmutPtr`；当外层被安全变成可变对象时，`WrappedPtr` 暴露出可变指针。

当算子拿到一个只读 `ColumnPtr` 并确实需要修改它时，必须经过 `Column::mutate(std::move(ptr))`：

```cpp
// be/src/column/column.h
static MutablePtr mutate(Ptr ptr) {
    MutablePtr res = ptr->try_mutate(); // ref==1 时不克隆，此时 use_count 临时变为 2
    ptr.reset();                        // 立刻释放旧句柄，use_count 降回 1
    res->mutate_each_subcolumn();       // 浅层遍历子列，对 _data/_null/_elements/_offsets 各自触发 COW
    return res;
}
```

注意这里有一个极易被忽略的细节：`try_mutate()` 返回 `res` 的瞬间，形参 `ptr` 还没析构，所以独占场景下 `_use_count` 会短暂从 1 跳到 2；紧接着 `ptr.reset()` 必须抢在 `res->mutate_each_subcolumn()` 和返回之前把引用计数降回 1，否则调用方拿到 `res` 后看到的计数仍是 2，下一次再 `mutate` 就会白白触发深拷贝。

此外，在确定对象生命周期安全且高频访问的热路径上，`Cow` 还提供了逃生舱 `as_mutable_raw_ptr()` / `as_mutable_ref()`，直接跳过原子引用计数的增减——但也正是这类逃生舱和存量代码的惯性，埋下了后面三座真实暗礁。

### 1.3 从落地到默认关闭：StarRocks COW 踩过的三座真实暗礁

如果只读论文和设计文档，COW Column 看起来近乎完美。但翻开 StarRocks 的真实提交历史，你会看到一条极具工程警示价值的演进曲线：引入 COW（`#56286`/`#56287`）→ 加固语义（`#66037`/`#66740`）→ 修复 `ChunkPtr` 冲突（`#66805`）→ 修复 `mutate` 后 Heap-Use-After-Free（`#67314`）→ 最终在 `#73480`（2026 年 5 月）将 `enable_cow_optimization` 默认值回退为 `false`（提交标题直白写着：`Disable COW optimization due to design flaws causing crashes`）。

这三次故障把“**把 COW 事后嫁接到存量可变执行引擎**”的全部深坑暴露得淋漓尽致：

1. **暗礁一：容器层与列层的双层所有权失明（`#66805`）**
   在 StarRocks 中，执行流水线传递的是 `ChunkPtr`（即 `std::shared_ptr<Chunk>`），`Chunk` 内部再持有一组 `ColumnPtr`。问题来了：如果一个 `ChunkPtr` 被 Multicast CTE 或缓存复制了一份，此时 `chunk_ptr.use_count() == 2`，但 `Chunk` 内部每一列 `Column` 依然只被这一个 `Chunk` 对象指向——**`column->use_count()` 赫然等于 `1`！** 消费者 A 对列调用 `try_mutate()` 时，只看 `Column` 自己的引用计数，误以为天下只有自己一个主人，直接原地改写了内存，把消费者 B 正在读取的同份 `Chunk` 彻底毁坏。只要 `Chunk` 壳层允许共享，单靠列层的引用计数根本无法判定独占性。
2. **暗礁二：包装列析构与裸指针悬空引发的 Heap-Use-After-Free（`#67314`）**
   早期实现里有一段典型的危险代码：`unfold_const_column(ColumnPtr&& column)` 先 `auto mut_column = Column::mutate(std::move(column))`，把常量列展开后返回 `const_column->data_column()->as_mutable_ptr()`；而在 `OlapTableSink::_validate_data` 中，先取出 `auto* column_ptr = chunk->get_column_raw_ptr_by_slot_id(...)`，再调用 `chunk->update_column(nullable->data_column(), desc->id())` 用内部数据列顶替掉外层的 `NullableColumn`。一旦 `chunk` 释放掉旧的 `NullableColumn`，先前持有的裸指针或未移交所有权的子列引用就会瞬间变成悬空指针，触发 UAF。
3. **暗礁三：复合列子列跨列别名与逃生舱失控（`#73480`）**
   看 `#73480` 中附带的极简复现用例 `test/sql/test_map/T/test_map_function_cow`：
   ```sql
   SELECT count(c1), count(c2), count(mp)
   FROM (SELECT *, map_keys(l.c2) mp FROM test_map l) t
   WHERE mp[1] + c1 < 1000 AND mp[1] + c1 > 100;
   ```
   `map_keys(l.c2)` 为了零拷贝，直接把 `MapColumn c2` 内部的 `_keys` 和 `_offsets` 子列指针装进了新构造的 `ArrayColumn mp` 里。现在同一个 `Chunk` 里同时住着 `c2` 和 `mp`：两个外层壳的引用计数都是 `1`，但它们肚子里的 `_keys` 和 `_offsets` 引用计数是 `2`。当后续 `WHERE` 谓词对整块 `Chunk` 执行过滤或对列调用原地变异时，只要链路上有一处存量代码用 `as_mutable_raw_ptr()` 绕开了 `try_mutate()`，或者 `mutate_each_subcolumn()` 只做了一层浅层检查而漏掉了深层嵌套（例如 `Nullable(Array(Nullable(String)))`），原地删减 `c2` 的 `_offsets` 就会把 `mp` 的偏移数组一并改乱，下一行读取立刻段错误崩溃。

**核心教训由此水落石出**：为什么 ClickHouse 和继承其内核的 Apache Doris 能够常年稳定运行 `COW<IColumn>`，而 StarRocks 事后改造却步步惊心？因为 ClickHouse 从第一天起就强制规定 **`Block`（容器）按值传递而非 `shared_ptr` 共享**，且**所有算子入口拿到的列默认都是只读 `ColumnPtr`**，全代码库不存在大面积绕过 COW 的存量 `mutable` 路径。而在一个已经演化了数年的大规模可变执行引擎上事后补装 COW，哪怕只有 1% 的旧算子还在悄悄调用 `as_mutable_raw_ptr()`，就足以击穿整个所有权契约。

### 1.4 开源、闭源与学术界引擎的六大内存所有权路线横评

跳出单一引擎，把视野拉到整个数据库生态，其实业界对“列内存所有权”分化出了四条截然不同的设计哲学：

| 引擎 / 体系 | 共享与保护粒度 | 写入前动作 | 嵌套列 / 切片共享处理 | 原子引用计数开销 | 核心工程权衡 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ClickHouse / Apache Doris** | 对象级（`COW<IColumn>`，`boost::intrusive_ptr`） | 显式 `IColumn::mutate(std::move(col))`，按值传递 `Block` 杜绝容器双层共享 | `WrappedPtr` 递归 `mutate` + 禁止隐式裸可变指针 | 每次列传递 / 拷贝均需原子增减 | 从 Day 1 强制不可变契约时非常优雅；跨线程高频传递短命列时原子操作有轻微缓存行争用 |
| **StarRocks** | 对象级（`Cow<Column>`，自研 `RCPtr`） | `Column::mutate()` + 存量逃生舱 `as_mutable_raw_ptr()` | `mutate_each_subcolumn()` 浅层解引用，遇 `ChunkPtr` 共享与 `map_keys` 别名易失守 | 提供 raw ptr 规避部分计数 | 事后改造存量可变引擎风险极高，目前默认关闭 `enable_cow_optimization` 回到显式防御拷贝 |
| **Meta Velox** | **Buffer 级**（`BufferPtr` / `AlignedBuffer`，挂接 `MemoryPool`） | 写前检查 `buffer->isUnique()`；不唯一则向 `MemoryPool` 分配新缓冲拷贝 | `Vector` 只是轻量外壳，Dictionary / Slice / Flat 全部共享底层 `BufferPtr`，天然杜绝壳层 ref=1 但数据共享的假象 | Buffer 级原子计数，但比对象树层次更扁平 | **公认最干净的 C++ COW 分层**：把所有权下沉到物理字节缓冲，算子可自由重组 `Vector` 视图而不破坏隔离 |
| **Apache Arrow / DataFusion / Trino** | **严格不可变**（Arrow `Buffer` / Trino `Block`） | **禁止原地写**；一切转换要么零拷贝 `slice()` / 挂过滤位图，要么用 Builder 构造新不可变列 | 零拷贝共享任意子数组与偏移切片，绝无写坏风险 | Rust `Arc` / Java GC 引用 | 安全性最高、天然跨进程/跨语言零拷贝 IPC；代价是稠密算子链路下临时内存分配与 GC/Allocator 压力偏高 |
| **DuckDB** | **算子局部独占**（`DataChunk`）+ 显式借用（`Vector::Reference`） | 算子输出 `DataChunk` 归属自身复用；过滤/投影靠 `SelectionVector` 间接索引延迟物化 | `VectorBuffer` 管理变长字符串堆生命周期，`UnifiedVectorFormat` 统一读取扁平/字典/常量向量 | 极大削减热路径原子引用计数 | 单机多核吞吐与 L1/L2 缓存局部性登峰造极；要求开发者严格遵守 `DataChunk::Reset()` 与借用生命周期纪律 |
| **Umbra / HyPer / Databricks Photon / Snowflake** | 流水线编译帧（Register / Tuple-at-a-time）或 Arena 微批独占 | 算子融合把列留在寄存器或紧致 L1 缓冲，物化边界（Hash/Sort）才写入专用 Arena | 编译期/微批边界静态划清所有权，极少跨算子共享可变列 | 几乎为零 | 性能天花板最高，但对工程架构（代码生成或严密算子状态机）要求也最重 |

面试或做架构评审时，一句最到位的总结是：**如果你从零设计 C++ 向量化内核，不要把 COW 建在复杂的 `Column` 对象继承树上，而应像 Velox 那样把 COW 下沉到平坦的 `BufferPtr` 字节层，再配合 DuckDB 式的 `SelectionVector` 延迟物化，尽可能连 `mutate()` 都不必触发。**

---

## 二、多列 Runtime Filter：跨 Exchange 的分区路由难题与全行业演进

内存拷贝解决的是“单机算子内不要做无用功”，而 Runtime Filter（运行时过滤器，下文简称 RF）解决的是“跨表连接时不要把注定匹配不上的 99% 垃圾行读进内存、更不要塞进网络 Shuffle”。

### 2.1 第一性原理：为什么单列 RF 很简单，一到多列 Shuffle Join 就立刻卡壳？

单列 Broadcast Join 的 RF 非常直觉：小表 `dim` 在每个节点构建完哈希表，顺手把连接键塞进一个 Bloom Filter / MinMax Filter，直接交给本机扫大表 `fact` 的 Scan 算子过滤即可。

但把场景换成开篇那条 **多列 Shuffle Join**：

```sql
SELECT ... FROM fact_events l
JOIN [SHUFFLE] dim_campaigns r
  ON l.campaign_id = r.campaign_id
 AND l.event_date  = r.event_date
```

立刻会出现两个完全不同、却经常在讨论中被混为一谈的技术难题：

#### 难题 A：多列分区路由的全局 RF（Multi-Column Partitioned Global RF）
在 Shuffle Join 中，右表 `dim_campaigns` 按 `hash(campaign_id, event_date) % N` 被打散到了 $N$ 个并发 Build 实例上。
- 如果 Merge Node 把这 $N$ 个实例的 Bloom Filter 强行按位或（OR）合并成**一个**全局大 Bloom Filter，不仅装载率暴涨导致假阳性率飙升，而且单个大过滤器的尺寸很容易突破 L3 Cache，探测性能断崖下跌。
- 因此 StarRocks 等现代 MPP 引擎采用**多分区过滤器（Multi-Partitioned Runtime Filter）**：Merge Node 不把 $N$ 个子过滤器混为一谈，而是按顺序拼接成一个数组 `_hash_partition_bf[0 .. N-1]`（每个子块是一个独立紧凑的 `SimdBlockFilter`），广播给所有 Scan 节点。
- **关键转折就在这里**：当 Scan 节点拿到这个分桶数组，准备在进入 `ExchangeNode` 之前提前过滤事实表行时，它怎么知道当前这一行应该去查第几个桶 `_hash_partition_bf[bucket_idx]`？
  - 如果连接键只有单列 `campaign_id`，`bucket_idx = hash(l.campaign_id) % N`，一列就够；
  - 可一旦分区键是双列 `(campaign_id, event_date)`，**Scan 节点必须同时取出 `l.campaign_id` 和 `l.event_date` 计算联合哈希 `hash(l.campaign_id, l.event_date) % N`，才能定位到正确的 `bucket_idx`**，然后再拿 `l.campaign_id` 去探查 `_hash_partition_bf[bucket_idx]`！只要缺失任何一个分区列，算出来的桶号就全错，必然导致**漏掉本该匹配的结果行（静默数据错误）**！

#### 难题 B：多个独立单列 RF vs 复合键联合过滤（Composite Joint Filter）
即便解决了跨 Exchange 路由，如果只为 `campaign_id` 和 `event_date` 各自建一个单列 Bloom Filter，在**列间强相关**场景下依然会彻底失效。看一个两行反例：
- Build 侧过滤后只剩两条合法组合：`(campaign_id = 101, event_date = 2026-11-22)` 和 `(campaign_id = 202, event_date = 2020-01-01)`；
- Probe 侧大表里有 10 亿条历史垃圾数据，全是 `(campaign_id = 101, event_date = 2020-01-01)`。
- 此时单列 RF 1 认为 `101 ∈ {101, 202}`（通过），单列 RF 2 认为 `2020-01-01 ∈ {2026-11-22, 2020-01-01}`（通过）——**两道独立单列过滤器合起来过滤率为 0%**！
- 只有构建**复合键联合过滤器**（把 `(c0, c1)` 联合哈希进同一个 Tuple Bloom Filter），`(101, 2020-01-01)` 才会被瞬间拦下。当然，复合键过滤器的代价是它彻底失去单列 Min/Max 区间语义，也无法下推到存储层单列 ZoneMap 索引。

### 2.2 StarRocks 源码拆解：跨 Exchange 门禁、多分区探测与存储下推红线

翻看 StarRocks 在 `#57269`（`Enable multi columns in global runtime filter by default`）及其周边链路的代码，能把工业级多列全局 RF 的完整工程边界看得一清二楚。

![多列全局 Runtime Filter 跨 Exchange 路由与下推边界，以及学术界 Predicate Transfer 演进](/images/diagrams/exec-multicolumn-rf-routing-and-pushdown.svg)

1. **FE 侧跨 `ExchangeNode` 的三段式决策（`ExchangeNode.canCrossExchangeNode`）**：
   并非所有多列 Join 的 RF 都值得推过 `ExchangeNode`，因为每多推一个 Global RF，Build 端就要多建一个过滤器、网络多传一份合并包、Scan 端还可能延长等待超时。StarRocks FE 划出了三条清晰分界：
   - **Broadcast Join 或单等值键（`equalCount == 1`）**：永远允许跨 `ExchangeNode` 下推；
   - **多等值键但按单列分区（`equalCount > 1 && partitionByExprs.size() == 1`）**：只推送 `probeExpr` 恰好等于那个唯一分区列的 RF，故意拦截其余列的 Global RF，避免多个 GRF 叠加大面积拖慢 Scan 等待时间；
   - **多等值键且按多列分区（`partitionByExprs.size() > 1`）**：校验全部 `partitionByExprs` 均为 `SlotRef`（保证可安全映射回下游 Scan 槽位，避免复杂表达式重复求值与类型漂移），且 `probeExpr` 属于其中之一，随后把完整的 `partitionByExprs` 列表绑定在 `RuntimeFilterDescription` 中一并下推。
2. **BE 侧两级组合过滤结构（`be/src/runtime/runtime_filter.h`）**：
   StarRocks 用 `ComposedRuntimeFilter` 将轻量级的 `MinMaxRuntimeFilter` 与成员测试类的 `RuntimeMembershipFilter`（子类包括 `TRuntimeBloomFilter`、`RuntimeBitsetFilter`、`RuntimeEmptyFilter`）封装在一起。执行 `evaluate()` 时先跑极低开销的 `MinMax` 筛掉区间外数据，再对幸存行计算多列联合分区哈希 `bucket_idx`，查 `_hash_partition_bf[bucket_idx].test_hash(hash)`。更细粒度地，若 Build 侧键空间跨度紧凑（`max - min` 较小），`RuntimeFilterFactory` 会自动升级为 `ComposedRuntimeBitsetFilter`，用零假阳性的紧凑位图取代 Bloom Filter。
3. **存储下推的硬红线（`be/src/exec/olap_scan_prepare.cpp`）**：
   在把 RF 转换为存储引擎 `SegmentIterator` 谓词时，源码里有四行极其关键的拦截：
   ```cpp
   // If the runtime filter's partition-by-exprs's size is greater than 1,
   // skip to push it down to storage engine.
   if (desc->num_partition_by_exprs() > 1) {
       continue;
   }
   ```
   **为什么非要在这里画死红线？** 因为列存引擎的 `SegmentIterator` 高度依赖延迟物化：读取第一列 `campaign_id` 的 Page 时，立刻用单列 `ColumnPredicate` / ZoneMap / Bitmap 索引剪枝，压根还没解码 `event_date` 列的 Page。如果强行把依赖 `(campaign_id, event_date)` 联合哈希的分区 RF 下推到单列谓词层，读第一列时根本算不出 `bucket_idx`！因此多列分区 RF 必须在 Scan 算子拼出包含所有分区列的 `Chunk` 后再执行过滤——虽然省不了读盘，但它依然把过滤挡在了 `ExchangeNode` 之前，省下了昂贵的跨节点网络 Shuffle、序列化和 Join Probe 哈希探测。
4. **倾斜 Join（Skew Join）与多列 RF 的协同陷阱**：
   在 `PlanFragment.java` 中还可以看到一个极隐蔽的细节：当优化器把一个倾斜 Shuffle Join 拆成“热点走 Broadcast Join + 非热点走 Shuffle Join”两条分支时，两条分支的 Build RF 数量必须严格对齐（`shuffleJoinNode.getBuildRuntimeFilters().size() == hashJoinNode.getBuildRuntimeFilters().size()`），否则若多列剪枝规则把 Shuffle 侧某个 RF 裁掉而 Broadcast 侧还留着，Merge Node 合并状态机就会陷入不一致或永久等待。

### 2.3 开源、闭源与学术界全景对照：从局部 RF 到 Predicate Transfer

各家引擎因为架构定位（单机/分布式、存储解耦深度、调度模型）不同，在动态过滤与多列过滤上走出了截然不同的工程重心：

| 引擎 / 论文路线 | 动态过滤载体 | 多列 / 跨节点处理方式 | 能否剪枝存储 I/O（文件/RowGroup） | 核心特点与取舍 |
| :--- | :--- | :--- | :--- | :--- |
| **StarRocks** | `MinMax` + `SimdBlockFilter` + `Bitset` + `InFilter` + `TopN` | 全局多分区数组 `_hash_partition_bf[bucket]` 跨 Exchange 携带全量 `partitionByExprs` | 单列/Broadcast RF 可下推 Segment/Parquet/Iceberg 文件；多列分区 RF 止步于 Scan Chunk 层 | 分布式跨节点链路极成熟，支持 Pipeline 级多分区与自适应超时降级 |
| **Apache Doris** | `IN` / `MIN_MAX` / `BLOOM_FILTER` / `IN_OR_BLOOM` | Coordinator/Merge Node 汇总全局 RF，按单列独立下推与自适应选择率监控（`runtime_filter_selectivity.h`） | 支持下推至存储 Segment 与外表分区/文件裁剪 | 动态监测过滤选择率，过滤率过低时运行期熔断止损，减少无效 CPU 探测 |
| **DuckDB** | `JoinFilterPushdownInfo`：`MinMax` + `InFilter` + `BloomFilter` + `PrefixRangeFilter` + `PerfectHashJoinFilter` | 单机多核共享 `DynamicTableFilterSet`，直接与 `JoinHashTable` 内部哈希桶/前缀范围耦合 | 深度下推至 `LogicalGet` 的 RowGroup MinMax 与 Storage Scan | 零网络通信成本，针对整型紧凑键可直接复用 Perfect Hash 与 Prefix Range 实现近乎免费过滤 |
| **Meta Velox** | `VectorHasher` 域值提取 + Range / Bloom / Negated Filter | `HashBuild` 提取键域推给本机同 Task `TableScan`；跨节点依赖上层引擎（Presto/Gluten）桥接 | 直接在 `TableScan` 解码前跳过 Parquet/DWRF RowGroup 与字典页 | 作为可嵌入执行库，局部紧耦合过滤极强，但分布式协调交由宿主框架 |
| **Trino** | Coordinator 收集动态 `Domain`（Range/Discrete Values） | Broadcast 走本机过滤；Partitioned Join 经 Coordinator 汇总回推至 Split 调度层（Dynamic Partition Pruning） | 强项在于**调度期直接剪掉整个 Hive/Iceberg Partition 与文件 Split** | 跨异构湖仓连接器通用性最好，但 Coordinator 汇总有控制面延迟，且大基数下易退化为 Range |
| **ClickHouse** | `BuildRuntimeFilterStep` / `BuildRuntimeFilterTransform`（新 Analyzer） | 侧重单机与局部 Join 过滤下推，复杂跨分片多列分区 RF 仍在演进中 | 支持推至 `MergeTree` 主键/索引预过滤 | 强项在单机极端吞吐，分布式多阶段动态过滤相比 MPP 数仓起步更晚 |
| **Databricks Photon / Snowflake / BigQuery** | Photon：Dynamic File Pruning（DFP）+ 大容量 Runtime Bloom Filter；Snowflake：Micro-Partition 元数据裁剪；BigQuery：Shuffle 阶段动态重平衡过滤 | Photon 突破原生 Spark 10 MB 限制构建大 Bloom Filter 裁剪 Delta 文件；Snowflake 借助 Micro-Partition 天然多列 Min/Max + Search Optimization 协同剪枝 | **全部把重炮打在云对象存储文件级 I/O 裁剪上** | 在存算分离云数仓里，省下一次 S3/OSS 文件读取的收益远大于省几微秒内存探测 |
| **学术界：LIP → Predicate Transfer → RPT+（2017–2025）** | 级联 Bloom Filter 网络（模拟 Yannakakis 半连接消元） | 不再局限于单次 Binary Join，而是在**整张多表连接图**上做前向/后向两趟 Bloom 过滤传递；RPT+（VLDB 2025，DuckDB 实证）引入非对称传递与动态管线，砍掉冗余构造并按运行期选择率自适应调节 | 级联过滤器可直接推到底层基表做块级与行级双重过滤 | 工业界当前大多仍停留在“单跳/局部多跳 RF”，**全局图级的 Robust Predicate Transfer 是下一代优化器与执行引擎融合的明确方向** |

---

## 三、分布式精确 `COUNT(DISTINCT)`：代数分解、倾斜打散与多列去重路线对决

如果说 COW Column 决定单机内存效率、Runtime Filter 决定 Join 前能不能少读少传，那么精确 `COUNT(DISTINCT)` 就是分布式聚合里最容易把 30 TB 集群直接打挂的“内存与网络粉碎机”。

### 3.1 第一性原理：为什么精确 `COUNT(DISTINCT)` 是分布式的“非结合律难题”？

普通聚合函数如 `SUM(x)` 或 `COUNT(x)` 满足**可分解性与结合律**：

$$\text{SUM}(A \cup B) = \text{SUM}(A) + \text{SUM}(B)$$

因此不管集群有 10 台还是 1000 台机器，每个节点都可以先在本地把 1 亿行压成 1 个数字，再通过一次微不足道的网络传输汇总。

但精确 `COUNT(DISTINCT d)` 违背了可加性：

$$|A \cup B| = |A| + |B| - |A \cap B| \neq |A| + |B|$$

除非每个节点保留完整的去重集合 $A$ 并把整个集合发过网络求并集（在千万/亿级基数下网络和内存立刻爆炸），否则执行引擎**必须在某个物理节点上把所有相同 `(g, d)` 的记录强制汇聚到一起完成去重，之后才能转变成可加的 `COUNT` / `SUM`**。

围绕“什么时候汇聚、按什么键汇聚、汇聚时热点炸了怎么办、同时有 $k$ 个不同列要去重怎么办”，工业界演化出了一整套精密的物理拓扑谱系。

![分布式精确 COUNT(DISTINCT) 执行拓扑谱系：3/4 阶段、倾斜打散与多列 Distinct 路线对决](/images/diagrams/exec-exact-count-distinct-topology-spectrum.svg)

### 3.2 单列精确 Distinct：二、三、四阶段与 Salting 的精确切换边界

以经典查询 `SELECT g, COUNT(DISTINCT d) FROM t GROUP BY g` 为例，结合 StarRocks FE `SplitMultiPhaseAggRule.java` 与 Hologres 30 TB 级查询实战，单列精确去重有四种递进形态：

1. **一 / 二阶段聚合（最优但最挑条件）**
   如果表的数据物理分布已经按 `g`（或 `(g, d)`）分桶/Colocate，或者完全没有 `GROUP BY` 且去重列恰好等于分桶键，则不需要重新打散：本地去重后直接本地计数（一阶段），或局部去重后汇总计数（二阶段）。
2. **三阶段聚合（1 次 Shuffle，怕 `g` 倾斜）**
   - **Stage 1（Local Agg）**：各节点先按 `(g, d)` 做本地预去重，消灭节点内重复项；
   - **Shuffle by `(g)`**：按分组键 `g` 重分布；
   - **Stage 2（Distinct Global）** + **Stage 3（Final Global）**：由于同一 `g` 的所有 `d` 都在同一个节点上，先按 `(g, d)` 完成全局去重，紧接着在**同一节点本地直连**（零额外网络）按 `g` 计数输出。
   - **盈亏判断**：当 `g` 的基数够高且分布均匀（例如按 `user_id` 分组算 `COUNT(DISTINCT item_id)`）时，三阶段只付 **1 次 Shuffle** 成本，速度最快。但只要 `g` 的基数很低（如 `GROUP BY country`、`GROUP BY status`）或存在严重倾斜（例如大量 `NULL` 或头部热点账号），整个热键对应的所有 `d` 都会砸向唯一一台机器的哈希表，引发长尾甚至 OOM。
3. **四阶段聚合（2 次 Shuffle，靠 `d` 的高基数自动拆散热点 `g`）**
   - **Stage 1（Local Agg）**：按 `(g, d)` 本地预去重；
   - **Shuffle 1 by `(g, d)`**：把分组键和去重键拼在一起哈希重分布——即使 `g = 'CN'` 占了 80% 数据，只要 `d = user_id` 有几亿个不同值，热键 `'CN'` 也会被均匀撒向全集群所有节点！
   - **Stage 2（Distinct Global）** + **Stage 3（Distinct Local）**：各节点对分到自己手里的 `(g, d)` 完成全局去重，并就地算出该节点上每个 `g` 的局部去重计数 `partial_cnt`；
   - **Shuffle 2 by `(g)`**：只传输收敛后的 `(g, partial_cnt)`——每个节点每个分组只有一行，网络流量几乎可以忽略；
   - **Stage 4（Final Global）**：对 `partial_cnt` 做普通 `SUM`，得到精确结果。
4. **分桶打散（Salting / Bucketization）——什么时候它反而是纯负优化？**
   在 Spark / Hive 调优里，工程师常手写 `GROUP BY g, hash(d) % B` 再在外层 `SUM` 来抗倾斜；StarRocks 也在 `GroupByCountDistinctDataSkewEliminateRule.java` 中内置了自动化改写（派生桶列 `mod(murmur_hash3_32(d), bucket_num)`）。
   但源码里一段名为 `shouldSkipRewriteSinceDistinctDistributedWell()` 的判断，点出了一个非常反直觉却至关重要的优化器洞察：
   - **如果优化器本就会选四阶段聚合（按 `(g, d)` Shuffle），且统计信息显示 `NDV(d) > bucket_num`**，那么按 `(g, d)` 做哈希分发本身就已经把数据打得比 `% bucket_num` 更散了；此时强行套一层 Salting 只会白白多算一次 `murmur_hash3_32`、多加一列分区键并破坏局部性，属于纯负优化！
   - **只有当候选计划可能走向三阶段按 `g` Shuffle（且存在热键 `g` 倾斜风险）时**，Salting 才有真实意义——或者当业务上存在极少数超级热键 `d`（比如未登录匿名 `user_id = 0` 占了全表 30% 行）导致连按 `(g, d)` 分区都倾斜时，需要更前置的 Local 去重（把单机内 1 亿条 `user_id = 0` 在进网络前压成 1 条），如果 Local 阶段因内存不足提前放弃预聚合（Streaming Agg Spill/Bypass），热键 `d` 才会穿透进网络打垮下游。

### 3.3 多列精确 `COUNT(DISTINCT a), COUNT(DISTINCT b), ...` 的四条工业路线对决

一旦一条 SQL 里出现多个不同的 `COUNT(DISTINCT)` 列（尤其是报表/BI/宽表分析中常见的 5–10 个维度去重），前面单列按 `(g, d)` Shuffle 的方案立刻失效——你不可能同时按 `(g, a)` 和 `(g, b)` 分发同一行数据。

为此，主流引擎发展出了四条截然不同的解题路线，各有清晰的适用边界：

1. **路线一：`Expand / Repeat` 行展开法（Apache Spark / Hologres / Hive 经典路径）**
   把每条输入行复制 $k$ 份，并附带一个虚拟列 `gid ∈ {0, 1, ..., k-1}`：第 0 份保留 `(g, a, NULL, gid=0)`，第 1 份保留 `(g, NULL, b, gid=1)`……如此一来，$k$ 个 Distinct 统一退化成了单列 `(g, val, gid)` 的普通去重，一套 3/4 阶段聚合就能一次算完。
   - **优点**：只扫一次基表，计划结构统一，天然复用所有单列抗倾斜机制。
   - **死穴**：在 30 TB 级大表上，$k$ 个 Distinct 意味着 Local Agg 输入与网络 Shuffle 量**线性放大 $k$ 倍**。当 $k \ge 4$ 且基数高导致 Local 预去重收敛率低时，网络带宽和堆内存会呈指数级恶化。
2. **路线二：专用 `MULTI_DISTINCT_COUNT` 算子（StarRocks `MultiDistinctByMultiFuncRewriter` / Apache Doris）**
   不复制任何输入行，直接把 `COUNT(DISTINCT a), COUNT(DISTINCT b)` 改写为专用聚合函数 `multi_distinct_count(a), multi_distinct_count(b)`。每个分组 `g` 的聚合状态内部维护各自的紧凑 `HashSet`（`phmap::flat_hash_set`），局部算完后序列化该 `HashSet` 发送到按 `g` 分区的 Global 节点做集合合并。
   - **优点**：完全消灭 $k$ 倍行膨胀；在单 Tablet（无 Shuffle）或 `g` 基数高、每组内 Distinct 元素很少（例如按订单/店铺分组）时极快。
   - **死穴**：如果全局无 `GROUP BY` 或 `g` 极度倾斜，所有节点的 `HashSet` 都会被序列化并汇入单点节点合并，极易 OOM；且难以直接支持多参数 `COUNT(DISTINCT a, b)`。
3. **路线三：CTE 共享扫描 + 拆分支独立聚合 + Join 合流（StarRocks `MultiDistinctByCTERewriter` / Trino `SPLIT_TO_SUBQUERIES`）**
   StarRocks 在 `RewriteMultiDistinctRule.java` 中给出的现代化默认答案（遇到多参数 `COUNT(DISTINCT a, b)`、带倾斜 Hint 或高开销场景时优先启用）：把基表扫描（含 Filter/Join 子树）抽成一次 `CTEProduce`，下游分叉出 $k$ 个 `CTEConsume`，每条分支只投影自己需要的那一列 `(g, a)` 或 `(g, b)`，各自独立走最优的 3/4 阶段聚合与倾斜治理，最后在顶端按 `g` 把结果 Join 拼成一行。
   - **优点**：既不发生 $k$ 倍行膨胀，也不把多张大 `HashSet` 堆进同一个节点，且每路分支的并发度、Shuffle 策略与多参数去重互不干扰。
   - **代价**：引入了 CTE 缓冲（内存/溢写）与末尾多路 Join 开销，因此仅当基表子计划开销大、或每路去重基数高到足以摊薄 CTE 物化成本时最划算。
4. **路线四：Trino `MarkDistinct` 与 DuckDB 公共输入共享基数表**
   - **Trino** 提供了业内最灵活的策略开关 `optimizer.distinct-aggregations-strategy`（`AUTOMATIC` / `MARK_DISTINCT` / `SINGLE_STEP` / `PRE_AGGREGATE` / `SPLIT_TO_SUBQUERIES`）。其中 `MarkDistinct` 算子在流过数据时维护按 `(g, d)` 的哈希集合，只给首次出现的行打上 `is_distinct = true` 布尔标志，下游 `COUNT` 只累加标记为 `true` 的行，避免破坏同一层里其他非 Distinct 聚合（如 `SUM(x)`）的语义。
   - **DuckDB** 在单机多核场景下通过 `DistinctAggregateCollectionInfo`（`src/include/duckdb/execution/operator/aggregate/distinct_aggregate_data.hpp`）做了一项极聪明的**表级共享去重**：如果一条 SQL 里同时出现 `COUNT(DISTINCT a)`、`SUM(DISTINCT a)`、`AVG(DISTINCT a)`，`table_map` 会把它们映射到**同一张 `RadixPartitionedHashTable`**，只做一次基数分区去重就服务全部同列 Distinct 聚合，把内存占用和探查开销直接砍掉大半。

### 3.4 存储协同与“降维打击”：当算法走到物理极限之后

当面对百亿行明细、超高基数（亿级 UV）和多维度自由上卷（Roll-up）时，纯执行引擎层面的 Shuffle 腾挪终究要向带宽和内存低头。生产级云数仓和 OLAP 引擎普遍准备了三把“降维武器”：

1. **全局低基数字典编码（Global Dictionary Encoding）**：在 StarRocks / Doris 中，对高重复字符串列（如 `device_id`、`city`）维护全局有序字典，将 `VARCHAR` 映射为紧凑 `INT32`。精确 `COUNT(DISTINCT)` 在执行期直接退化为整型哈希或定长位图，内存占用缩小 5–10 倍，且无字符串哈希与解引用开销。
2. **Roaring Bitmap 预物化与可结合合并**：把不可加的 `COUNT(DISTINCT user_id)` 提前编码成精确的 `BITMAP` 列（StarRocks/Doris 的 `BITMAP_UNION` + `BITMAP_COUNT`，Snowflake 官网强推的 `BITMAP_AGG` + `BITMAP_COUNT`）。一旦进入 Bitmap 域，去重操作重新获得了**完美的结合律与幂等律**（$\text{Bitmap}(A \cup B) = \text{Bitmap}(A) \lor \text{Bitmap}(B)$），不仅彻底消灭 3/4 阶段聚合与多列膨胀，还能直接用于物化视图的增量合并与任意维度 Roll-up（前提是 ID 可映射为连续/紧凑整型；若为稀疏 UUID 则需先走字典映射）。
3. **两级外存哈希与近似 Sketch 逃生舱**：ClickHouse 的 `uniqExact` 底层采用两级哈希表（Two-Level Hash Table），内存逼近阈值时按哈希前缀桶溢写磁盘并归并，死守单机精确性；而 BigQuery 与 Snowflake 在面对超大规模交互式 UV 统计时，都会在文档与优化建议中明确引导业务区分“财务级精确（走 Exact / Bitmap）”与“趋势级分析（走 HyperLogLog++ `APPROX_COUNT_DISTINCT`）”，用常数级（几 KB 到几十 KB）内存换取亚秒级响应和近乎零网络压力。

---

## 四、把三条主线收束成一张内核设计与排障决策总表

回到开篇那条 30 TB 查询。当我们在真实线上集群里审视执行 Profile 时，这三块内核机制往往彼此咬合、互相放大：COW 处理不好会让过滤和投影阶段把 CPU 烧在拷贝上；多列 RF 没跨过 Exchange 会让海量垃圾行涌进 Shuffle，直接引爆后续精确 `COUNT(DISTINCT)` 的内存 OOM；而多列 Distinct 选错 `Expand` 路线又会让本就脆弱的 `Chunk` 内存池雪上加霜。

下面这张决策矩阵可用于架构选型与线上 Profile 排障速查：

| 排障现场 / 设计目标 | 核心根因判断 | 优先选用的工程解法 | 必须警惕的副作用与反例 |
| :--- | :--- | :--- | :--- |
| **执行期 `clone()` / `memcpy` 占比异常高** | 变长列/嵌套列跨算子防御性深拷贝 | 若自研新引擎：走 **Velox Buffer 级 COW（`isUnique`）+ DuckDB `SelectionVector` 延迟物化**；若用对象级 COW：强制容器按值传递、封死 `as_mutable_raw_ptr` 逃生舱 | 切忌在 `shared_ptr<Chunk>` 共享容器与存量裸可变路径并存的老引擎里硬开对象级 COW（警惕 `#66805` / `#73480` 式别名崩溃） |
| **多列 Shuffle Join 下 Scan 吐出海量行打满网络** | 全局 RF 未跨 `ExchangeNode`，或跨过后过滤率极低 | 启用**多列分区 Global RF**（携带全量 `partitionByExprs` 算 `bucket_idx`）；若两列高度相关且单列选择率差，考虑**复合键 Tuple Bloom Filter** 或推动存储按组合键聚类/分区 | 多列分区 RF 不可下推单列存储索引（违反延迟物化）；且当 `partitionByExprs` 仅 1 列时别盲推其余列 GRF，防 Scan 等待时间反增 |
| **多表星型/雪花连接中间结果爆炸** | 传统双表局部 RF 无法在多跳间提前消元 | 推进 **LIP / Robust Predicate Transfer（RPT+）**，在连接图上用非对称 Bloom 传递提前收缩基表，并加运行期低选择率熔断 | 切忌对高选择率（过滤掉 <10%）或超高 Build 基数表硬建 Bloom Filter，构造与探测开销会反超收益 |
| **单列 `COUNT(DISTINCT)` 某单个节点 OOM / 极度长尾** | 走了三阶段按 `g` Shuffle 且 `g` 存在低基数/热点倾斜 | 切**四阶段按 `(g, d)` Shuffle**；若仍留在三阶段或受限引擎（Spark/Hive），加 **Salting 分桶打散**；确保 Local 阶段未过早跳过预聚合 | 若计划已走四阶段且 `NDV(d) > B`，再叠 Salting 属于纯冗余开销 |
| **多列 `COUNT(DISTINCT a, b, c, ...)` Shuffle 流量与内存雪崩** | `Expand/Repeat` 引发 $k$ 倍数据膨胀，或 `multi_distinct` 汇聚到单点 | 大表/高基数/倾斜场景优先走 **CTE 拆分 + 独立 3/4 阶段聚合 + Join 合流**（或 Trino `SPLIT_TO_SUBQUERIES`）；小表/单 Tablet/极高 `g` 基数走 `multi_distinct`；稳定报表直接沉淀 **全局字典 + Roaring Bitmap** | CTE 拆分会增加一次物化与末尾 Join 成本；若输入表本身很小，反而比 `multi_distinct` 或 `Expand` 慢 |

三道深水区问题绕了一大圈，最终都指向同一条底层工程铁律：**数据库内核里没有“无条件更好”的银弹优化，只有被清晰界定了所有权不变量、路由前提和成本盈亏边界的受控机制。** 写代码如此，设计下一代湖仓与 AI 数据计算引擎亦复如是。

---

## 源码基线与参考资料

- **本地核验源码快照**：
  - StarRocks（`a9a9fd7`）：`be/src/common/cow.h`、`be/src/column/column.h`、`be/src/bench/cow_bench.cpp`、`be/src/runtime/runtime_filter.h`、`be/src/exec/olap_scan_prepare.cpp`、`fe/fe-core/.../planner/ExchangeNode.java`、`fe/fe-core/.../rule/transformation/SplitMultiPhaseAggRule.java`、`GroupByCountDistinctDataSkewEliminateRule.java`、`RewriteMultiDistinctRule.java`；历史提交 `#56286`（`cb3f101`）、`#56287`（`f4a3242`）、`#57269`（`227a69c`）、`#66805`（`c9511ca`）、`#67314`（`c1913b9`）、`#73480`（`8dbc74b`）。
  - ClickHouse（`c824bb6`）：`src/Common/COW.h`、`src/Processors/QueryPlan/BuildRuntimeFilterStep.h`。
  - Apache Doris（`5ff9558`）：`be/src/core/cow.h`、`be/src/exec/runtime_filter/runtime_filter_selectivity.h`。
  - DuckDB（`538d289`）：`src/include/duckdb/common/types/vector.hpp`、`src/include/duckdb/execution/operator/join/join_filter_pushdown.hpp`、`src/include/duckdb/execution/operator/aggregate/distinct_aggregate_data.hpp`。
  - Apache DataFusion（`c17c87c`）：Arrow 不可变 `RecordBatch` / `Array` 内存模型。
- **竞品公开资料与学术文献**：
  - Meta Velox 文档：Vector & `BufferPtr` Ownership / `isUnique` 与 `HashBuild` Dynamic Filter Pushdown。
  - Trino 文档：Dynamic Filtering & Dynamic Partition Pruning、`optimizer.distinct-aggregations-strategy`（`MARK_DISTINCT` / `PRE_AGGREGATE` / `SPLIT_TO_SUBQUERIES`）。
  - Databricks Photon（SIGMOD 2022）与 Dynamic File Pruning / Runtime Bloom Filter；Snowflake `BITMAP_AGG` 与 Micro-Partition Pruning；Google BigQuery `APPROX_COUNT_DISTINCT` 与 Shuffle 树聚合。
  - Jianqiao Zhu et al., *Looking Ahead Makes Query Plans Robust* (LIP, SIGMOD 2017)；Yifei Yang et al., *Predicate Transfer: Efficient Pre-Filtering on Multi-Join Queries* (CIDR 2024)；*Robust Predicate Transfer with Dynamic Execution* (RPT / RPT+, VLDB 2025)。

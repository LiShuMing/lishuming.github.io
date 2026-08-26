---
title: "【源码】深入 Hash Join：从内存哈希表到分区 Spill"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
slug: "dive-hash-join"
categories:
  - 数据库
  - 大数据
tags:
  - Hash Join
  - StarRocks
  - Apache Doris
  - ClickHouse
  - DuckDB
  - Spill
description: "结合 StarRocks、Apache Doris、ClickHouse 与 DuckDB 源码，分析 Hash Join 的键编码、哈希表布局、Build/Probe、并行化、Join 语义、Runtime Filter，以及分区 Spill 与恢复机制。"
draft: false
---

## 1. 引言：Hash Join 不只是一次哈希查找

Hash Join 在数据库系统中有着举足轻重的地位。尤其是在 StarRocks、Apache Doris、ClickHouse、DuckDB 等分析型数据库中，它通常是等值连接最重要的执行算法。

教科书对 Hash Join 的描述非常简洁：选择较小的一侧构建哈希表，再用另一侧逐行查表。在哈希函数近似均匀、Build 侧能够放入内存时，时间复杂度接近 `O(N + M)`。但真正的工程实现远比这个模型复杂：

- Join Key 是单个整数、定长组合键，还是需要序列化的变长键？
- 一个 Key 对应一行还是多行，重复链如何组织？
- Inner、Outer、Semi、Anti、Mark Join 如何共享同一条 Probe 主路径？
- 多线程如何并行 Build，又如何避免让 Probe 为并行付出额外代价？
- 哈希表占满内存后，哪些状态可以撤销，哪些状态不能直接落盘？
- Build 与 Probe 如何使用完全一致的分区规则，保证匹配行最终相遇？
- 单个热点 Key 大到任何分区都装不下时，递归分区是否仍然有效？

这些问题决定了 Hash Join 究竟是一个只在小表上很快的算法，还是一个能支撑复杂分析负载、在内存压力下仍然可靠的执行系统。

本文基于以下本地源码版本展开分析：

| 项目 | 源码提交 | 重点实现 |
| --- | --- | --- |
| StarRocks | [`0fd27fd4`](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 自适应哈希表、Pipeline、可撤销内存、分区 Spill |
| Apache Doris | [`5202d06d`](https://github.com/apache/doris/tree/5202d06dd8feb3390ff32839227eeee89c345b57) | 模板化 Join 语义、Partitioned Hash Join、递归重分区 |
| ClickHouse | [`c631f591`](https://github.com/ClickHouse/ClickHouse/tree/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23) | Hash/Parallel Hash/Grace Hash Join、多形态 Key |
| DuckDB | [`1c4ecd81`](https://github.com/duckdb/duckdb/tree/1c4ecd8138ae0c63c73957a411e65484301bb300) | 行式 Tuple 布局、Radix 分区、External Hash Join、Perfect Hash Join |

本文关注执行引擎的共性与差异。Spark 的 Join 选择、`HashedRelation` 与 AQE 已在《[深入 Spark Hash Join](/2026/08/25/dive-spark-hash-join/)》中单独讨论，这里不再重复。

## 2. 先说结论

对四套实现做完横向分析后，可以得到八个关键判断：

1. **Hash Join 的核心成本不是哈希计算，而是 Build 侧的物化、索引和重复行组织。** SQL 只写了一个 Join，执行器实际维护的是键、Payload、桶数组、冲突链、匹配标记、NULL 状态以及 Runtime Filter。
2. **高性能实现不会只有一种哈希表。** 单个小整数可以直接映射，稠密整数范围可以按偏移寻址，定长组合键可以打包，变长组合键才需要序列化或使用宽哈希值。
3. **哈希表通常只保存“定位信息”，数据本体由独立的行块或列块持有。** 这样既能降低桶的宽度，也便于向量化 Gather；代价是 Spill 后必须重新构建指针关系。
4. **Spill 的本质是把一次不可控的大 Join，转换成多次有内存上界的小 Join。** Build 与 Probe 必须使用同一 Hash、同一分区位和同一层级，否则正确性无法成立。
5. **不能简单把内存中的哈希表原样写盘。** 桶中往往包含地址、Arena 引用或行指针；落盘的是逻辑行或列块，恢复时再建立新的本地哈希表。
6. **四个引擎都在实现 Hybrid/Grace Hash Join 的变体，但调度策略不同。** StarRocks 尽量一次装入多个分区；Doris 以 `(build_file, probe_file, level)` 队列递归处理；ClickHouse 动态倍增 Bucket；DuckDB 根据内存预留挑选一组当前可 Build 的分区。
7. **数据倾斜是分区 Spill 的硬边界。** 如果一个热点 Key 自身就超过内存预算，继续使用同一 Hash 的更多位并不能把相同 Key 拆开。
8. **Runtime Filter、直接映射和 Perfect Hash Join 说明优化不能只停留在“选中 Hash Join”。** 真正有效的优化是减少进入 Probe 的行、减少键编码开销，并让 Build 侧内存模型可预测。

最重要的认识是：

> Hash Join 不是“Build 一张表再 Probe”这么简单，而是一套围绕数据布局、内存所有权、并发调度与外存恢复建立起来的协议。

## 3. 从 SQL 到内存对象

考虑一个典型查询：

```sql
SELECT o.order_id, c.region
FROM orders o
JOIN customers c
  ON o.customer_id = c.customer_id;
```

若优化器选择 `customers` 为 Build 侧，逻辑过程如下：

```text
Build side: customers
  ├─ 计算 customer_id 的 Hash
  ├─ 保存 Join Key 与输出所需 Payload
  └─ 建立 Hash Bucket -> Build Row 的索引

Probe side: orders
  ├─ 批量计算 customer_id 的 Hash
  ├─ 定位 Bucket
  ├─ 校验完整 Key，排除 Hash 冲突
  └─ Gather Build Payload，拼接输出
```

### 3.1 内存不能只按原始数据量估算

Build 侧哈希表的近似内存可以写成：

```text
M_build ≈ M_key
        + M_payload
        + M_bucket/control
        + M_duplicate_link
        + M_null_and_match_flags
        + M_runtime_filter
        + M_allocator_fragmentation
```

因此，“Build 表原始数据只有 2 GiB”并不意味着 2 GiB 内存足够。低装载因子的桶数组、对齐、字符串 Arena、重复链和输出列物化都可能把实际占用放大。

另一个常被忽略的量是输出规模：

```text
JoinOutput = Σ count_probe(k) × count_build(k)
```

Hash Join 只能让查找接近常数时间，无法消除多对多 Join 的组合爆炸。一个高频 Key 同时出现在两侧时，即使哈希表能够放入内存，输出也可能成为真正瓶颈。

### 3.2 Build 与 Probe 之间是一份状态契约

Build 阶段至少要向 Probe 阶段交付：

- Join Key 的编码方式；
- 哈希函数与桶布局；
- Build 行或 Payload 的定位方式；
- NULL 是否参与匹配；
- 重复 Key 的遍历方式；
- Outer Join 所需的命中标记；
- 分区与 Spill 的元信息。

这份状态一旦建立，Probe 就不能随意更换 Hash 或 Key 编码。External Hash Join 只是把这份契约从“一张全局哈希表”扩展到“按分区反复建立的局部哈希表”。

## 4. 键编码决定哈希表形态

数据库很少把所有 Join Key 都转换成通用对象。通用表示便于编程，却会引入虚函数、分支、间接访问、重复 Hash 和额外内存。

更常见的选择是：

| Key 形态 | 典型表示 | 优点 | 风险 |
| --- | --- | --- | --- |
| 单个小整数 | 原值或直接映射下标 | 无序列化，可能消除 Hash | 值域过大时浪费空间 |
| 稠密整数区间 | `key - min_key` | 一次数组寻址 | 依赖可靠的 Min/Max 与稠密度 |
| 多个定长字段 | 打包为 128/256 bit | 一次比较，少间接访问 | 需要处理 NULL 位与对齐 |
| 单 String | String 引用或专用 String Key | 避免通用序列化 | 字符串生命周期必须稳定 |
| 复杂变长组合键 | 序列化或宽 Hash + 回表比较 | 覆盖任意类型 | CPU、Arena 与碰撞校验成本高 |

### 4.1 StarRocks：Key Constructor 与 Hash Method 解耦

StarRocks 在 [`join_hash_table.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table.cpp) 中把两个问题分开处理：

1. `JoinKeyConstructorType` 决定如何从列中构造 Key，例如 `ONE_KEY`、`SERIALIZED_FIXED_SIZE` 和通用序列化；
2. `JoinHashMapMethodType` 决定如何由 Key 定位行，例如 `DIRECT_MAPPING`、`RANGE_DIRECT_MAPPING`、`DENSE_RANGE_DIRECT_MAPPING`、`LINEAR_CHAINED` 和 `BUCKET_CHAINED`。

这意味着“Key 是什么”和“如何索引 Key”可以独立优化。对值域足够小的整数，直接映射可能比通用 Hash 更合适；对唯一 Build Key，`*_SET` 变体可以省掉重复链；无法命中特化条件时，再退回通用 Bucket Chaining。

### 4.2 ClickHouse：从数值键到加密哈希键

ClickHouse 的 [`chooseMethod()`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.cpp#L304) 会根据实际列类型选择 Map：

- 单个数值键使用 `key8`、`key16`、`key32`、`key64` 等专用类型；
- 多个定长键总宽度不超过 16 或 32 字节时，打包为 `keys128` 或 `keys256`；
- 单个 String/FixedString 使用专用路径；
- 其他情况使用无歧义序列化值的宽 Hash 表示。

对应的 Value 也并非一种：[`HashJoin.h`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.h#L357) 区分 `RowRef`、`RowRefList` 和 `AsofRowRefs`，分别服务于单行、重复行和 ASOF Join。

### 4.3 DuckDB：Pointer Table 与 TupleDataCollection

DuckDB 的 [`JoinHashTable`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/include/duckdb/execution/join_hashtable.hpp) 把 Build 行放在 `TupleDataCollection` 中，Pointer Table 的 Entry 保存行指针和部分 Hash Salt。

Probe 时先检查 Salt，只有 Salt 相符的候选才做完整 Key 比较。碰撞使用线性探测，而相同 Key 的多行通过 Tuple 尾部的 Next Pointer 串起来。这种设计有三个效果：

1. Bucket Entry 保持紧凑，先用 Salt 过滤掉大量不可能匹配的行；
2. Key 与 Payload 采用统一 Tuple 布局，匹配后可以向量化 Gather；
3. 数据本体与 Pointer Table 分离，External Join 可以分区保存 Tuple，再为当前分区重建 Pointer Table。

## 5. 重复 Key、NULL 与 Join 语义

### 5.1 重复 Key 不是边缘情况

对 Inner Join，一个 Probe Key 可能命中多个 Build Row。执行器不能假设一批输入会在一次调用中完整输出，因为结果可能超过向量或 Chunk 的最大行数。

因此 Probe 状态通常要保存两个位置：

```text
probe_row_index  -> 当前处理到哪一条 Probe Row
build_match_ptr  -> 该 Probe Row 的重复链处理到哪里
```

DuckDB 的 `ScanStructure::AdvancePointers()` 会沿 Next Pointer 继续遍历；Doris 的 Probe 模板保存 Probe/Build 索引，在输出 Block 已满时挂起，下次继续。这是向量化 Hash Join 能够处理一对多结果的关键。

### 5.2 Join Type 改变的是状态，不只是输出格式

不同 Join Type 对哈希表提出不同要求：

| Join Type | Probe 侧要求 | Build 侧附加状态 |
| --- | --- | --- |
| Inner | 输出所有匹配组合 | 重复链 |
| Left Outer | 无匹配时输出 Probe 行 + NULL | 通常无需全表扫描 |
| Right Outer | Probe 后输出未匹配 Build 行 | Build Row 命中标记 |
| Full Outer | 两侧未匹配行都要输出 | Build Row 命中标记 + Probe 未命中处理 |
| Semi | 只判断是否存在匹配 | 可使用 Set/Unique 优化 |
| Anti | 只保留不存在匹配的 Probe 行 | NULL 语义尤其重要 |
| Mark | 输出 `TRUE/FALSE/NULL` 标记 | 需要记录 Build 侧 NULL 与关联统计 |

Doris 在 [`hashjoin_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_probe_operator.cpp) 中通过 `ProcessHashTableProbe<JoinOpType>` 模板为多种 Join 语义生成专用路径。DuckDB 则在 Tuple 布局中为 Right/Full Join 追加 `found` 布尔位，并对 Correlated Mark Join 维护分组计数。

### 5.3 NULL 需要服从 SQL 三值逻辑

普通 `=` 下，`NULL = NULL` 不是 `TRUE`。Build 阶段通常可以过滤不可能匹配的 NULL Key，但 Right/Full Outer Join 仍然必须保留这些 Build 行，以便最后输出未匹配结果。

如果谓词是 `IS NOT DISTINCT FROM`，NULL 又可以彼此匹配。于是 NULL 处理不能只写在 Hash 函数里，而必须同时进入：

- Key 编码；
- Build 行保留策略；
- Probe 匹配判断；
- Mark/Anti Join 的三值逻辑；
- Spill 分区后的恢复逻辑。

## 6. 并行 Build 与 Pipeline 化 Probe

Hash Join 天然存在 Build Barrier：Probe 必须等到可用的 Build 状态建立后才能开始。现代执行引擎主要优化 Barrier 两侧的并行度，而不是假装它不存在。

### 6.1 两种常见并行 Build 方案

```text
方案 A：线程本地 Hash Table
  local build ─┐
  local build ─┼─► merge/finalize ─► shared probe
  local build ─┘

方案 B：按 Hash Shard 并行 Build
  input block ─► hash scatter ─► shard 0 / shard 1 / ...
                                  └─ 各线程独占部分 Bucket
```

线程本地表写冲突少，但 Finalize/Merge 可能昂贵；共享表省掉合并，但要控制并发插入、扩容和内存分配竞争。

ClickHouse 的 [`ConcurrentHashJoin`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/ConcurrentHashJoin.h) 给出了一个很有代表性的折中：Build 时按 Hash 把 Bucket 分给不同 Slot，每个线程只修改自己的 Two-Level Map Bucket；Build 完成后把 Bucket 移入公共 Map，Probe 线程读取同一张共享表，从而避免每批 Probe 数据再次 Scatter。

### 6.2 Probe 的向量化流水线

一批 Probe 数据通常经历：

```text
表达式计算
  -> NULL 过滤/选择向量
  -> 批量 Hash
  -> Bucket 定位
  -> Salt/Key 比较
  -> 重复链展开
  -> Build Payload Gather
  -> Residual Predicate
  -> 输出 Chunk
```

这里有两个性能重点：

1. 先使用 Selection Vector 压缩候选，再做昂贵比较；
2. 匹配阶段只传递 Build Row 的位置，最后按输出列 Gather，避免提前复制完整行。

## 7. 为什么 Hash Table 不能直接 Spill

当内存不足时，一个看似自然的想法是“把哈希表写入磁盘，稍后读回来”。但内存哈希表常包含：

- 指向 Arena 或 Block 的裸指针；
- 依赖当前地址空间的 Next Pointer；
- 根据 Capacity 构造的 Bucket 数组和 Bitmask；
- 与当前分配器绑定的字符串内存；
- Build Row 的并发命中标记。

这些对象即使序列化成功，恢复到不同地址后也未必有效。而且随机读取磁盘 Bucket 会把一次顺序扫描变成大量随机 I/O。

所以主流实现 Spill 的不是 Bucket，而是**可重新构建哈希表的逻辑数据**：

```text
In-memory Hash Table
  └─ revoke
      └─ logical build rows / column blocks
          └─ hash partition
              └─ sequential spill files

restore one partition
  └─ rebuild local Hash Table
      └─ read matching probe partition
          └─ normal vectorized probe
```

这也解释了为什么可 Spill Hash Join 不是给普通 Hash Join 增加一个 `write()` 方法，而是额外建立一套分区、文件、异步 I/O、恢复和状态调度系统。

## 8. Grace/Hybrid Hash Join 的正确性基础

设分区函数为：

```text
partition_id = P(hash(join_key), level)
```

Build 行和 Probe 行只有在满足以下不变量时才会进入同一分区：

```text
Hash_build(key) == Hash_probe(key)
P_build(hash, level) == P_probe(hash, level)
```

一个完整的外存 Join 通常分成五步：

1. Build 数据按 Hash 分区；
2. 内存允许时保留部分分区，其余顺序落盘；
3. Probe 数据使用相同规则分区；
4. 对每个分区恢复 Build 数据并重建局部哈希表；
5. 读取对应 Probe 分区执行普通 Probe，完成后释放该局部状态。

如果某个 Build 分区仍然太大，则增加分区位或更换层级，再对 Build/Probe 两侧同步重分区。这就是 Recursive Grace Hash Join。

Hybrid 的含义是：并非所有数据都必须落盘。能够留在内存的分区直接完成 Join，可以少写一次 Build、少写一次 Probe，也少读两次文件。

### 8.1 源码阅读坐标：四层架构不要混在一起

阅读四套代码时，最好始终区分以下四层，否则很容易把“算法选择”“Pipeline 调度”“哈希索引”和“Spill 文件”混成一个概念：

| 系统 | 规划/算法层 | Pipeline/控制层 | In-Memory Hash Table | External/Spill 层 |
| --- | --- | --- | --- | --- |
| StarRocks | FE 下发 `THashJoinNode` 与分布方式 | `HashJoinBuild/ProbeOperator`、`HashJoiner` | `JoinHashTable`、`JoinHashMap<LT, CT, MT>` | `SpillableHashJoinBuild/ProbeOperator`、Partitioned Spiller |
| Apache Doris | `TPlanNode`、Join Distribution/Op | Build Sink/Probe Operator 与 Local State | `JoinHashTable<Key, Hash, DirectMapping>` | `PartitionedHashJoin*Operator`、`SpillRepartitioner` |
| ClickHouse | `chooseJoinAlgorithm()`、`TableJoin` | `IJoin` 与 Joining Transform | `HashJoin`、`MapsOne/All/Asof` | `GraceHashJoin`、`FileBucket` |
| DuckDB | `PhysicalHashJoin` | Sink/Combine/Finalize/Operator/Source | `JoinHashTable`、Pointer Table、TupleData | Radix Partition、`ProbeSpill`、External Source State |

后续四章都按同一顺序展开：先给出入口和对象关系，再分析 Hash Table 布局，然后进入 Build/Probe 热路径，最后讨论 Spill 状态机。这样能够看清“相同问题如何被不同架构拆分”，而不仅是对照类名。

## 9. StarRocks：从可撤销哈希表到多分区恢复

StarRocks 的普通 Hash Join 由 `JoinHashTable` 承担数据结构选择，Pipeline Build/Probe Operator 负责调度。可 Spill 路径位于：

- [`spillable_hash_join_build_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_build_operator.cpp)
- [`spillable_hash_join_probe_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_probe_operator.cpp)

### 9.1 架构路径：Operator、Controller 与数据结构分层

StarRocks 没有把所有逻辑都塞进 `HashJoinNode`。Pipeline 执行路径可以沿下面的对象关系阅读：

```text
HashJoinBuildOperator                 HashJoinProbeOperator
  │ push_chunk(build)                   │ push_chunk(probe) / pull_chunk()
  ▼                                     ▼
HashJoiner  <────── build dependency ───────┐
  │                                        │
  ├─ HashJoinBuilder                       ├─ HashJoinProber
  │    ├─ SingleHashJoinBuilder            │    ├─ SingleHashJoinProberImpl
  │    └─ AdaptivePartitionHashJoinBuilder │    └─ PartitionedHashJoinProberImpl
  │                                        │
  └────────────── JoinHashTable ───────────┘
                    │
                    └─ JoinHashMap<LogicalType,
                                   KeyConstructor,
                                   HashMapMethod>
```

各层职责非常明确：

- `HashJoinBuildOperator` 和 `HashJoinProbeOperator` 对接 Pipeline 的 `push/pull/set_finishing` 协议；
- [`HashJoiner`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_joiner.h) 更像 Controller，维护依赖、阶段、Runtime Filter、残余谓词和 Lazy Materialization；
- [`HashJoinBuilder/HashJoinProber`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_join_components.h) 决定单表还是 Cache-Aware 分区表；
- `JoinHashTable` 负责 Build 数据所有权、Key/Map 特化和 Join 语义；
- `JoinHashMap<LT, CT, MT>` 是最终进入热循环的模板实例，`LT`、Key 构造方式和 Map 实现都已静态确定。

`HashJoiner` 的阶段不是注释意义上的概念，而是控制 Pipeline 可执行操作的状态机：

```cpp
// 简化自 hash_joiner.h
enum HashJoinPhase {
    BUILD,       // 只允许追加 Build Chunk
    PROBE,       // Hash Table 已只读，允许 Probe
    POST_PROBE,  // 扫描未匹配/已匹配的 Build Row
    EOS
};

bool has_post_probe(join_type) {
    return RIGHT_OUTER || RIGHT_ANTI || FULL_OUTER;
}
```

Build Operator 在 `set_finishing()` 中完成 `build_ht()`、Runtime Filter 构建并唤醒 Probe Dependency；Probe Operator 第一次获得输入时，通过 `_reference_builder_hash_table_once()` 引用只读表。Right/Full 类 Join 在 Probe 输入结束后进入 `POST_PROBE`，调用 `probe_remain()` 输出 Build 侧剩余行。

因此 StarRocks 的 Barrier 是：

```text
build push
  -> build_ht
  -> create runtime filters
  -> notify probe dependency
  -> probe references read-only HT
  -> probe push/pull
  -> optional post-probe scan
```

### 9.2 JoinHashTable：列数据、行号链和哨兵行

`JoinHashTable` 的数据本体仍是列式 `build_chunk`，哈希索引主要由 `first` 和 `next` 两个 `uint32_t` 数组构成：

```text
build_chunk
  row 0      : dummy/default row
  row 1..N   : real build rows, columnar storage

first[bucket] = bucket chain 的第一条 build row id
next[row_id]  = 同一 bucket 的下一条 build row id
0             = 链表结束，同时可表示 outer join 的 NULL build row
```

第 0 行是一个关键约定。`_init_build_column()` 为每个 Build Column 先追加 Default 值；之后真实数据从 Row ID 1 开始。这样 Probe 输出 `(probe_row, build_row=0)` 时，可以直接从 Default/Nullable Column 取到 Outer Join 所需的 NULL，而不必在热循环中为“未匹配”维护完全不同的数据路径。

源码中的内存统计也揭示了真实 Hash Table 的组成：

```cpp
// 简化自 JoinHashTable::mem_usage()
usage  = build_chunk->memory_usage();         // Build 数据本体
usage += first.capacity() * sizeof(uint32_t); // Bucket head
usage += next.capacity()  * sizeof(uint32_t); // Row chain
usage += build_pool->reserved_bytes();        // 变长 Build Key
usage += probe_pool->reserved_bytes();        // Probe 临时序列化 Key
usage += build_key_column->memory_usage();
usage += build_slice.size() * sizeof(Slice);
```

这说明 StarRocks 的 Map Cell 并不保存完整 Payload。哈希表给出 Build Row ID，输出阶段再从列式 `build_chunk` 按索引 Gather。其优点是索引紧凑、输出列可裁剪；代价是 Build Chunk 生命周期必须覆盖整个 Probe/Post-Probe，并且 Spill 时不能只写 `first/next`。

### 9.3 两级选择：先构造 Key，再选择索引结构

[`JoinHashMapSelector`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table.cpp#L154) 的设计重点是把 Key 表示与 Hash Map 形态解耦。

第一步 `_determine_key_constructor()`：

```cpp
// 伪代码，分支与源码一致
if (只有一个普通等值 Key) {
    if (String 实际最大宽度 <= 4/8/16 && 末尾不存在歧义 0 字节)
        return SERIALIZED_FIXED_SIZE_INT/BIGINT/LARGEINT;
    return ONE_KEY;
}

for (每个组合 Key) {
    if (定长连续类型，或可证明为短 String)
        total_bytes += field_bytes + null_safe_flag_byte;
    else
        return SERIALIZED_VARCHAR;
}
return pack_to_32/64/128_bits_when_possible;
```

这里 `NULL-safe equal` 要额外写入空值标记，否则 `NULL` 与底层默认值会被编码成同一个 Key。短 String 特化还要排除尾部 `\0` 可能造成的编码歧义。

第二步 `_determine_hash_map_method()`：

```cpp
// 简化自 _determine_hash_map_method()
if (BOOLEAN/TINYINT/SMALLINT)
    return DIRECT_MAPPING;

if (ONE_KEY && (INT || BIGINT))
    if (value range 足够小且内存模型更优)
        return RANGE_DIRECT_MAPPING / DENSE_RANGE_DIRECT_MAPPING;

if (bucket size 在实现上限内)
    return LINEAR_CHAINED[_SET];

return BUCKET_CHAINED; // 通用回退；ASOF 使用专用 chained 版本
```

Range Direct Mapping 不是只比较 `max-min` 与行数。源码把 L2/L3 Cache 和 Join Type 一起放入成本判断：

- Semi/Anti 且没有 Other Conjunct 时，只需表达“Key 是否存在”，可以用 Bitset Set；
- 普通 Join 要保存重复关系，比较 Direct Map 的值域内存与 `first/next` 成本；
- Dense Range 使用约 2 bit 状态表示值域位置，再保留按行的 `first`，用于兼顾稠密度和重复行。

最终 Build 通过双重 Dispatch 生成具体模板：

```cpp
// 简化自 JoinHashTable::build()
tie(key_constructor, map_method) = selector.construct(...);

dispatch_join_hash_map(key_constructor, map_method, [&]<CUT, MUT>() {
    using LT = logical_type_of<CUT>;
    using CT = key_constructor_of<CUT>;
    using MT = map_method_of<MUT>;
    hash_map = make_unique<JoinHashMap<LT, CT, MT>>(table_items, probe_state);
});

hash_map->build_prepare(state);
hash_map->probe_prepare(state);
hash_map->build(state);
```

外层运行时只做一次 Variant Dispatch，内层 Probe 热路径拿到的是确定的 `LT/CT/MT`，避免逐行判断 Key 类型和 Map 类型。

### 9.4 Probe：可暂停的重复链与延迟物化

`SingleHashJoinProberImpl` 对一批 Probe Chunk 保存三个核心对象：

- `_probe_chunk`：原始 Probe 列；
- `_key_columns`：表达式计算后的 Join Key；
- `_current_probe_has_remain`：当前批次是否还有重复匹配未输出。

```cpp
// 简化自 hash_join_components.cpp
push_probe_chunk(chunk) {
    _probe_chunk = chunk;
    prepare_probe_key_columns(_key_columns, _probe_chunk);
}

probe_chunk() {
    hash_table->probe(keys, probe_chunk, output, &has_remain);
    filter_probe_output_chunk(output);      // Other/Where Conjunct
    lazy_output_chunk(output);              // 最后补齐延迟列
    if (!has_remain) _probe_chunk = nullptr;
}
```

`has_remain` 让哈希表能够在输出 Chunk 达到容量时暂停重复链，下次 `pull_chunk()` 继续，而不重新 Hash 已处理的 Probe Row。Residual Predicate 先在只包含必要列的中间 Chunk 上计算，只有通过过滤的行才物化 Lazy Column，这对宽表 Join 尤其重要。

Right Outer、Right Anti 和 Full Outer 的 Build 命中状态不能在普通 Probe 输出结束时丢弃。它们通过 `POST_PROBE` 再扫描哈希表，`probe_remain()` 分批输出命中或未命中的 Build Row。

### 9.5 Adaptive Partition Hash Join：为 Cache 分区，不是为 Spill 分区

StarRocks 还有一个容易与 External Spill 混淆的 [`AdaptivePartitionHashJoinBuilder`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_join_components.cpp#L512)。它的目标不是把数据写盘，而是让若干子 Hash Table 尽量落入 L2/L3 Cache。

源码成本模型估算一次 Probe 会触碰：

```text
8 bytes(first + next)
+ Join Key 比较字节
+ Build 输出列字节
```

同时估算把 Probe Row Scatter 到 16 个分区的成本。只有“减少 Cache Miss 的收益”大于“Probe Shuffle 的代价”时，才保留分区模式：

```text
BUFFERING
  ├─ 行数进入收益区间 ─► APPENDING，按 CRC32 + fmix32 分到 16 个 Builder
  └─ 最终不划算 ──────► 合并成 SingleHashJoinBuilder

APPENDING
  ├─ 每 8 个 Chunk 重新估算每行 HT 字节
  └─ 收益消失 ────────► _convert_to_single_partition()
```

Broadcast Join 还会根据最大 Probe DOP 下调 Cache Miss Factor，因为多个 Prober 共享同一张表，跨核 Cache 复用会改变分区收益。

因此 StarRocks 实际存在两套“Partition”：

| 机制 | 触发目标 | 数据位置 | 是否重建 Hash Table |
| --- | --- | --- | --- |
| Adaptive Partition Hash Join | 改善 L2/L3 Locality | 全部在内存 | 每个 Cache Partition 一张子表 |
| Spillable Hash Join | 回收内存并保证查询继续 | 内存 + Spill File | 每个恢复批次重建局部表 |

### 9.6 Spill 不是默认路径，而是内存撤销后的模式切换

Build 初期仍走普通内存哈希表。收到内存撤销请求后，Operator 把策略切换为 `SPILL_ALL`。第一次切换时需要完成两个动作：

1. 根据当前 `ht_mem_usage()` 与 Spill MemTable 阈值估算分区数量；
2. 通过 `_convert_hash_map_to_chunk()` 把已建哈希表恢复为可分区的 Chunk，再与后续输入一起 Spill。

这一步很重要：系统没有在查询开始时就为最坏情况支付全部 Spill 成本，而是在真正发生内存压力时，把已经建立的不可撤销结构转换成可撤销数据。

Build Spill 采用按列格式，并给数据追加分区 Hash。列式写出更符合 StarRocks 的 Chunk 模型，也便于压缩和恢复。

### 9.7 Probe 侧复用 Build 分区布局

Probe Operator 从 Build Spiller 获取完整分区列表，并用它初始化 Probe Spiller。新到达的 Probe Chunk 计算相同 Hash 后有两条路径：

- 对应 Build 分区已经选入当前处理集合：直接送给相应 Prober；
- 对应 Build 分区尚未加载：写入相同 Partition ID 的 Probe Spill。

于是内存分区可以边接收 Probe 边执行，落盘分区等后续恢复。这正是 Hybrid Hash Join 的效果。

### 9.8 一次加载多少分区由可用内存决定

`_acquire_next_partitions()` 不只选择一个分区。它优先处理仍在内存中的 Build 分区，再按可用字节选择一个或多个落盘分区。对每个选中的分区分别创建 Builder/Prober，异步读取 Build 数据并重建局部哈希表。

```text
available memory
  ├─ partition 1: 420 MiB ─► local HT 1
  ├─ partition 7: 180 MiB ─► local HT 2
  └─ partition 9: 90 MiB  ─► local HT 3

probe rows ─► partition id ─► prober 1 / 2 / 3 / spill
```

这让小分区可以合批并行恢复，降低“一次只处理一个小分区”造成的 CPU 和 I/O 空闲。

### 9.9 Spill 与 Runtime Filter 的冲突

普通 Build 完成后，StarRocks 可以发布 IN Filter 或 Bloom Filter。但 Spill Build 当前会发布空 Runtime Filter。源码注释给出的原因很实际：若要构建全局 Filter，需要重新读取全部 Spill 数据，或者在切换前就知道完整 Build 规模。

这揭示了一个普遍矛盾：

> Runtime Filter 希望尽早看到完整 Build Key 集合，Spill 希望尽早释放 Build 状态；两者对生命周期的要求相反。

可行方向包括分区级 Filter、可增量合并的 Bloom Filter，或在分区元数据中维护低成本摘要，但都要权衡误判率、发布时间和额外内存。

## 10. Apache Doris：把递归 Spill 做成显式状态机

Doris 的普通 Build/Probe 路径位于 [`hashjoin_build_sink.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_build_sink.cpp) 与 [`hashjoin_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_probe_operator.cpp)。可 Spill 路径由 Partitioned Hash Join 包装普通 Hash Join：

- [`partitioned_hash_join_sink_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_sink_operator.cpp)
- [`partitioned_hash_join_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_probe_operator.cpp)

### 10.1 架构路径：外层 Operator 与内层 Hash Join Kernel

Doris 的普通路径可以按下面的调用链阅读：

```text
HashJoinBuildSinkOperatorX
  └─ HashJoinBuildSinkLocalState::process_build_block()
       ├─ 执行 Build Key 表达式
       ├─ init_hash_method<JoinDataVariants>()
       ├─ try_convert_to_direct_mapping()
       └─ ProcessHashTableBuild<HashTableCtx>::run<JoinOp>()
            └─ JoinHashTable<Key, Hash, DirectMapping>::build()

HashJoinProbeOperatorX
  └─ HashJoinProbeLocalState
       └─ ProcessHashTableProbe<JoinOp>::process<HashTableCtx>()
            ├─ init_serialized_keys()
            ├─ JoinHashTable::find_batch<JoinOp>()
            ├─ 生成 probe_indexs/build_indexs
            ├─ Gather 两侧输出列
            └─ Other Join Conjunct / Mark Conjunct
```

这里使用了两次正交的模板化：

- `HashTableCtx` 固化 Key 类型、Hash Method、是否 Nullable/Serialized/Direct Mapping；
- `JoinOp` 固化 Inner、Outer、Semi、Anti、Null-Aware 等语义。

`std::visit` 只发生在 Block 级入口，真正的逐行热循环已经是具体模板实例。这与 StarRocks 的 Key Constructor/Map Method Dispatch 目标相同，但 Doris 把 Join Type 也更直接地下沉到 `ProcessHashTableBuild/Probe` 模板。

可 Spill 路径不是替换这个 Kernel，而是在外面再包一层：

```text
PartitionedHashJoinSinkOperatorX
  ├─ 未 Spill：转发给 inner HashJoinBuildSinkOperatorX
  └─ 已 Spill：Build Block -> partition -> SpillFile[]

PartitionedHashJoinProbeOperatorX
  ├─ 未 Spill：转发给 inner HashJoinProbeOperatorX
  └─ 已 Spill：Probe Block -> partition -> SpillFile[]
                 -> recover one pair
                 -> recreate inner build/probe operators
```

### 10.2 JoinHashTable：`first/next/visited` 三个数组

Doris 的 [`JoinHashTable`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/common/hash_table/join_hash_table.h) 是理解普通 Join 的最短入口。它没有把 Payload 塞进 Cell，而是使用 Build Block 行号：

```cpp
// 源码数据成员的语义化摘录
DorisVector<uint32_t> first;   // bucket -> first build row
DorisVector<uint32_t> next;    // build row -> next row in bucket
DorisVector<uint8_t>  visited; // Right/Full/Semi/Anti 的逐行命中状态
const Key* build_keys;          // 完整 Key，碰撞后做相等比较
```

Build Block 同样保留第 0 行作为 Mock Row，真实行从 1 开始。`prepare_build()` 根据 Join Type 决定是否分配 `visited`：

```cpp
// 简化自 prepare_build<JoinOpType>()
bucket_size = DirectMapping
    ? force_bucket_size
    : normalize((num_elem + 1) * 8 / 7); // 预留装载率空间

first.resize(bucket_size + 1); // 额外一个位置保存 NULL Key 链头
next.resize(num_elem);

if (FULL_OUTER || RIGHT_OUTER || RIGHT_ANTI || RIGHT_SEMI)
    visited.resize(num_elem);
```

插入本身非常直接：

```cpp
for (uint32_t row = 1; row < num_elem; row++) {
    uint32_t bucket = bucket_nums[row];
    next[row] = first[bucket];
    first[bucket] = row;
}

if (!keep_null_key)
    first[bucket_size] = 0; // bucket_size 这个额外槽专门表示 NULL 链
```

因此 Doris 的冲突处理是 Separate Chaining，但链节点不是堆对象，而是稠密 `uint32_t next[]`。这比每行分配 Node 更节省内存，也让 Prefetch 和顺序访问更可控。

Direct Mapping 时，Bucket 已由 Key 唯一确定，`_eq()` 可以直接返回 `true`；普通 Hash 路径仍要用 `build_keys[row]` 做完整比较，避免 Hash 冲突产生错误匹配。

### 10.3 Build：Key 初始化、NULL 编码与批量插入

Build 的关键入口是 [`process_build_block()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_build_sink.cpp#L586)。可以把它拆成六步：

```cpp
// 结构化伪代码，保留源码调用关系
convert_overflow_columns(block);                    // String/Variant 物理格式稳定化
extract_join_column(block, null_map, raw_ptrs);    // 计算并拆出 Build Key
hash_table_init(state, raw_ptrs);                  // 选择 HashMethod/DirectMapping

visit(hash_table_variant, join_op, [&](ctx, op) {
    ProcessHashTableBuild<Ctx> process(rows, raw_ptrs, ...);
    process.run<op>(ctx, null_map,
                    &has_null_in_build_side,
                    short_circuit_for_null,
                    have_other_join_conjunct);
});
```

`ProcessHashTableBuild::run()` 内部再做：

```text
prepare_build<JoinOp>()
  -> 如有必要，把单列 NULL 替换为默认值
  -> init_serialized_keys(..., is_build=true)
  -> 计算 bucket_nums
  -> hash_table.build(keys, bucket_nums, rows, keep_null_key)
```

NULL 有两种表示策略：

- Null-Safe Equal 需要把 NULL 编入 Key，使 `NULL <=> NULL` 可匹配；
- 普通等值 Join 用外部 Null Map 排除 NULL，并在 Null-Aware/Outer 等特定语义下保留 NULL 行。

`short_circuit_for_null` 是 Null-Aware Anti Join 的重要优化：当语义允许仅凭 Build 侧存在 NULL 就确定结果时，不再无意义地构建完整哈希表。

### 10.4 Probe：行号向量把“搜索”与“输出”分开

[`ProcessHashTableProbe<JoinOp>`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/join/process_hash_table_probe.h) 不会在命中时立即复制整行，而是先填两个索引向量：

```text
probe_indexs = [0, 0, 2, 5, ...]
build_indexs = [7, 9, 3, 0, ...]

(0, 7), (0, 9) -> Probe Row 0 命中两个 Build Row
(5, 0)         -> Probe Row 5 未命中，Outer Join 输出 NULL Build Row
```

随后 `probe_side_output_column()` 与 `build_side_output_column()` 才按索引批量 Gather。若 Probe 索引连续、整批已消费且没有未完成的 Build 重复链，源码甚至直接转移 Probe Column 所有权，避免一次复制。

Join Type 在 `find_batch<JoinOp>()` 中编译期分派：

```cpp
if constexpr (INNER || LEFT/RIGHT/FULL_OUTER)
    find_batch_inner_outer_join<JoinOp>();
else if constexpr (LEFT_SEMI || LEFT_ANTI || NULL_AWARE_LEFT_ANTI)
    find_batch_left_semi_anti<JoinOp>();
else if constexpr (RIGHT_SEMI || RIGHT_ANTI)
    mark_build_rows_visited();
```

Inner/Outer 的主循环可以概括为：

```cpp
while (build_idx && output_not_full) {
    if (probe_key == build_keys[build_idx]) {
        probe_indexs.push(probe_idx);
        build_indexs.push(build_idx);
        if constexpr (RIGHT/FULL) visited[build_idx] = 1;
    }
    build_idx = next[build_idx];
}

// Left/Full 未命中时写 build_idx=0，复用 Mock Row
```

`HashJoinProbeLocalState::_probe_index` 和 `_build_index` 保存跨 Block Pull 的断点。当一条 Probe Row 的重复链产生超过 Batch Size 的结果时，下一次调用从同一个 `build_idx` 继续，而不是重新查 Bucket。

Residual Conjunct 会使流程多一层：先产生候选 `(probe_idx, build_idx)`，再执行非等值条件，最后根据结果修正 Outer/Semi/Anti 语义。换言之，Hash 只负责 Equi-Key 候选集，SQL 的完整 `ON` 条件仍在其后。

### 10.5 正常路径与 Spill 路径复用同一个内核

Partitioned Operator 初始仍把输入交给内部普通 Hash Join。发生内存撤销后，Build/Probe 数据按配置的 Partition Count 分区并写入 Spill File。

恢复时，Doris 为当前 Build Partition 建立内部 Runtime State，再复用普通 Hash Join 完成：

```text
recover build partition
  -> internal HashJoin build
  -> stream corresponding probe partition
  -> internal HashJoin probe
  -> release
```

这种“外层负责 External 调度，内层保持 In-Memory 内核”的分层减少了两套 Join 语义不一致的风险。Inner、Outer、Semi、Anti 等复杂语义仍由同一个 Probe 模板完成。

### 10.6 工作队列显式保存递归层级

Build 和 Probe 初次 Spill 完成后，系统把对应文件加入处理队列。队列项可以抽象成：

```text
PartitionTask {
  build_file,
  probe_file,
  level
}
```

如果恢复后的 Build Partition 仍然无法在预算内建表，`repartition_current_partition()` 会创建下一层的 Build/Probe Spill File，使用新的分区层级对两侧同时重分区，再把子任务放回队列。

```text
level 0 partition too large
  ├─ level 1 / subpartition 0
  ├─ level 1 / subpartition 1
  ├─ ...
  └─ level 1 / subpartition N-1
```

递归深度由 `spill_repartition_max_depth` 限制。达到最大深度仍无法放入内存时，系统返回明确错误，而不是无限生成 Spill 文件。

### 10.7 恢复状态机与二次内存撤销

`_pull_from_spill_queue()` 将每个队列项处理成一个小型 Pipeline：

```text
SETUP PARTITION
  -> 分批 read build_file 到 recovered_build_block
  -> 达到 spill_buffer_size 时主动 yield
  -> 文件读完后创建 inner RuntimeState/BuildSink/ProbeOperator
  -> 用 recovered_build_block 完成 inner build

PROBE PARTITION
  -> 分批 read probe_file 到 queue_probe_blocks
  -> push 给 inner ProbeOperator
  -> pull output
  -> inner EOS 后释放当前分区，处理下一项
```

这里的 `yield` 是关键设计：恢复文件不能在一次调度中无限读取，否则既占用 Pipeline Worker，也可能在 Hash Table 建立前就把整个分区物化进内存。

更进一步，Doris 允许在恢复 Build Partition 的中途再次收到内存撤销。此时 `repartition_current_partition()` 必须同时处理两部分数据：

```text
already recovered rows in _recovered_build_block
+ unread remainder behind _current_build_reader
```

源码先把已恢复 Block 路由到下一层子分区，再让 `SpillRepartitioner` 从 Reader 的当前位置继续，避免从文件头重读导致重复行。Probe 文件也使用同一新 Level 和 Fanout 重分区，最后把所有子文件重新放回工作队列。

恢复前的内存预留也显式包含：

```text
spill I/O baseline
+ first[bucket_count] * 4
+ next[build_rows] * 4
+ optional visited[build_rows]
+ serialized key estimate
```

这比只按 `recovered_build_block.allocated_bytes()` 申请内存更接近真正的 Build 峰值。

### 10.8 Spill 后为什么停用 Runtime Filter

从普通 Hash Join 切换到 Partitioned 模式时，`_revoke_unpartitioned_block()` 会调用 `runtime_filter_producer_helper->skip_process()`。原因与 StarRocks 类似：原先的 Build 状态被拆散到文件，继续发布一个未覆盖全部 Build Key 的 Filter 会产生假阴性，直接破坏正确性。

这也说明 Runtime Filter 不是 Hash Join 的附属统计，而是带正确性约束的 Build 输出。要在递归 Spill 中保留它，需要 Filter 本身能够跨 Partition/Level 无损合并，并且只有全局完成后才能作为排除型过滤器发布。

### 10.9 可观测性围绕退化路径设计

Doris 暴露 `SpillMaxPartitionLevel`、分区总数、恢复 Build/Probe 行数与耗时等指标。相比只记录 Spill Bytes，这些指标更接近问题本质：

- Level 持续升高，通常意味着分区估算不足或数据倾斜；
- Build 恢复时间高，可能是磁盘吞吐、解压或重建哈希表成为瓶颈；
- Probe 恢复行数远大于有效输出，说明过滤能力或 Build/Probe 选择可能不佳。

## 11. ClickHouse：Hash、Parallel Hash 与 Grace Hash

ClickHouse 将不同资源假设直接呈现为不同 Join Algorithm：

| 算法 | 核心假设 | 主要特点 |
| --- | --- | --- |
| `hash` | Build 侧能驻留内存 | 通用内存 Hash Join |
| `parallel_hash` | 内存充足，希望并行 Build | Two-Level Map 分桶并行构建 |
| `grace_hash` | Build 侧可能超过内存 | Hash 分桶、临时文件与逐桶恢复 |

### 11.1 从 Planner 到 `IJoin`：算法是显式策略对象

ClickHouse 的选择入口位于 [`chooseJoinAlgorithm()`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Planner/PlannerJoins.cpp#L1259)。Planner 不只生成一个枚举，而是创建实现统一 `IJoin` 接口的策略对象：

```cpp
// 简化自 tryCreateJoin()
switch (algorithm) {
case DIRECT:          return tryDirectJoin(dictionary_or_key_value_storage);
case HASH:            return make_shared<HashJoin>(...);
case PARALLEL_HASH:   return make_shared<ConcurrentHashJoin>(..., max_threads);
case GRACE_HASH:      return make_shared<GraceHashJoin>(initial_buckets,
                                                        max_buckets, ...);
case PARTIAL_MERGE:   return make_shared<MergeJoin>(...);
case FULL_SORTING_MERGE:
                       return make_shared<FullSortingMergeJoin>(...);
case AUTO:            return supported_merge
                            ? make_shared<JoinSwitcher>(...)
                            : make_shared<HashJoin>(...);
}
```

`parallel_hash` 还会参考右侧大小估计和 `parallel_hash_join_threshold`；`grace_hash` 则显式携带初始/最大 Bucket 数和临时存储 Scope。因此 ClickHouse 的架构不是“一种 Hash Join 内部自动切换所有模式”，而是：

```text
Planner/TableJoin
  -> chooseJoinAlgorithm
      -> IJoin
          ├─ HashJoin
          ├─ ConcurrentHashJoin (内部包含多个 HashJoin)
          └─ GraceHashJoin      (当前 Bucket 内部包含一个 HashJoin)
```

`ConcurrentHashJoin` 和 `GraceHashJoin` 都复用 `HashJoin` 作为内存内核：前者解决 Build 并行，后者解决外存分桶。

### 11.2 `RightTableData`：Map、Block 与 Arena 的所有权

ClickHouse 的 [`HashJoin::RightTableData`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.h#L390) 把索引与右表数据分开：

```cpp
// 语义化摘录
struct RightTableData {
    Type type;                        // key32/key64/keys128/hashed/...
    vector<MapsVariant> maps;         // ON 中每个 OR disjunct 一张 Map
    ScatteredColumnsList columns;     // 右表 Block 的列所有权
    NullmapList nullmaps;             // Right/Full 的 NULL/ON 未命中行
    Arena pool;                       // String Key、RowRefList 扩展节点
    size_t rows_to_join;
    size_t keys_to_join;
};
```

`MapsVariant` 的 Value 由 Join Strictness 决定：

```text
MapsOne  : Key -> RowRef       // ANY，只保留一个右表引用
MapsAll  : Key -> RowRefList   // ALL，保留同 Key 的全部行
MapsAsof : Key -> AsofRowRefs  // ASOF，Key 下再维护可检索结构
```

`RowRef` 不是复制 Payload，而是引用 `ColumnsInfo + row_num`。`ScatteredColumns` 还保留 Selector，允许 Concurrent Hash Join 的不同 Slot 共享原始 Block，只记录本 Slot 拥有哪些行。

这个所有权模型解释了三个源码现象：

1. 右表 Block 必须一直保存到 Probe/未匹配行输出结束；
2. String Key 和重复链扩展节点由 `Arena` 统一释放，避免大量小对象析构；
3. Right/Full Join 的 NULL Key 不能插入普通 Map，却要通过 `nullmaps` 留到最后输出。

### 11.3 Key Method、Build 与 Probe 的双重 Dispatch

`chooseMethod()` 先根据右侧实际列选择 Key Map：

```cpp
if (one_numeric_key)       return key8/key16/key32/key64;
if (all_fixed && bytes<=16) return keys128;
if (all_fixed && bytes<=32) return keys256;
if (one_string)             return key_string;
if (one_fixed_string)       return key_fixed_string;
return hashed; // 对复杂组合键生成无歧义序列化后的 UInt128 Hash
```

Build 入口 [`addBlockToJoin()`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.cpp#L636) 的核心路径是：

```text
materialize right columns
  -> 提取所有 ON disjunct 的 Key Column
  -> 保存需要输出的右表列为 ScatteredColumns
  -> 从 Key 中拆出 NullMap
  -> joinDispatch(kind, strictness, map)
  -> HashJoinMethods<kind, strictness, map>::insertFromBlockImpl()
```

这里 `joinDispatch()` 同时分派 `JoinKind × JoinStrictness × MapsOne/All/Asof`，所以具体插入函数知道是否只留一行、是否保留重复、是否需要 Used Flag。

Probe 入口 `joinBlock()` 以同样方式分派：

```cpp
joinDispatch(kind, strictness, maps, [&](auto kind, auto strictness, auto typed_maps) {
    result = HashJoinMethods<kind, strictness, TypedMaps>::joinBlockImpl(
        left_block, columns_to_add, typed_maps);
});
```

这种模板爆炸换来的收益是热循环没有 `switch(join_kind)`。同时 `JoinResultPtr` 允许一次 Left Block 产生多次输出：`next()` 返回当前结果、未完成的 Block 和 `is_last`，用于控制 ALL Join 的重复展开。

Build 完成后还有一个数据相关优化：如果 `ALL INNER/LEFT` 的右侧 Key 实际唯一，`all_values_unique` 会保持为真，系统可以把 ALL 提升为 RightAny 路径，消除无意义的 `RowRefList` 遍历。

### 11.4 ConcurrentHashJoin：Build 分片，Probe 尽量共享

[`ConcurrentHashJoin`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/ConcurrentHashJoin.cpp) 创建最多 256、且为 2 的幂的 Slot。Build Block 先计算与 Hash Map 一致的 Hash，再路由到不同 Slot：

```text
right block
  -> calculate hash/bucket
  -> selector = bucket & (slots - 1)
  -> ScatteredBlock[slot]
  -> slot-local HashJoin::addBlockToJoin()
```

Two-Level Map 把一张逻辑 Map 划成固定数量的 Submap Bucket。每个 Slot 只写 `slot, slot+N, slot+2N...` 这些 Bucket，因此 Build 阶段不需要让所有线程争用同一张普通 Hash Map。

`onBuildPhaseFinish()` 不是逐行重新插入，而是把其他 Slot 的 Submap Bucket 移到 Slot 0：

```cpp
for (slot = 1; slot < slots; ++slot) {
    for (bucket = slot; bucket < NUM_BUCKETS; bucket += slots)
        common_map.impls[bucket] = move(slot_map.impls[bucket]);

    common_columns.splice(slot_columns); // Block 所有权也转移
}

for (slot = 1; slot < slots; ++slot)
    slot.maps = common_maps;              // Probe 共享同一逻辑 Map
```

因此它试图实现“Build 时分治，Probe 时共享”。如果使用的是 Two-Level Map，Probe Block 无需再次按 Slot Scatter，所有线程可读公共 Map；无法使用 Two-Level 的特例仍需把 Probe Block 路由到各内部 HashJoin。

Right/Full Join 还要合并各 Slot 的 NullMap 和 Used Flag，否则同一右表未匹配行可能被重复输出。这部分状态合并说明 Parallel Hash Join 的难点不只在并发插入，还包括 Build 完成后的语义状态归并。

### 11.5 Grace Hash Join 保留当前 Bucket，其余落盘

[`GraceHashJoin.h`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/GraceHashJoin.h) 对三个阶段有非常清楚的说明：

1. Build Block 按 Key Hash 拆到多个 Bucket；当前 Bucket 加入内存 `HashJoin`，其余写临时文件；
2. Probe Block 同样拆分；当前 Bucket 直接 Probe，其余写对应左侧文件；
3. 逐个读取后续 Bucket 的右侧数据重建 Hash Join，再读取左侧数据完成 Probe。

```text
right/build block
  ├─ bucket 0 ─► in-memory HashJoin
  ├─ bucket 1 ─► right temp file 1
  └─ bucket N ─► right temp file N

left/probe block
  ├─ bucket 0 ─► probe now
  ├─ bucket 1 ─► left temp file 1
  └─ bucket N ─► left temp file N
```

### 11.6 超限时动态倍增 Bucket

如果当前内存 Hash Join 超过限制，`rehashBuckets()` 将 Bucket 数量翻倍，并受 `max_num_buckets` 约束。因为 Bucket 数保持 2 的幂，增加一位 Hash 即可细分原分区。

动态倍增有两个优点：

- 初始数据不大时，不必创建大量小文件；
- 真正超限后再增加分区，能够根据运行时规模适配。

它也带来状态复杂度：并发线程可能持有旧 Bucket Snapshot，源码需要检测 Bucket 数变化并重新 Scatter 当前 Block；早先写入旧 Bucket 的行，也可能在后续阶段再次分发到新 Bucket。

源码中的溢出处理比“Bucket 数乘二”多两次关键重建：

```text
current in-memory HashJoin overflow
  -> rehashBuckets(): bucket_count *= 2
  -> releaseJoinedBlocks(): 从 HashJoin 取回逻辑右表 Block
  -> 用新 bucket_count 重新 Scatter
  -> 当前 Bucket 的行重建一个新 HashJoin
  -> 其他 Bucket 写各自临时流
```

这再次印证：扩分区时迁移的是逻辑 Block，不是带地址的 Hash Map Bucket。

### 11.7 临时文件是 Join 状态的一部分

每个 `FileBucket` 同时拥有左、右临时流，并在 `WRITING_BLOCKS`、`JOINING_BLOCKS`、`FINISHED` 状态间转换。临时文件使用 Native Block 流和压缩配置，并记录压缩/未压缩字节以及 Join 临时文件数。

这不是外围 I/O 工具，而是 Join 状态机的一部分：只有在 Bucket 停止写入后，才能安全切换为读取并 Build；只有其非匹配 Build 行输出完成后，才能释放当前 Hash Join。

后续 Bucket 由 `getDelayedBlocks()` 串联处理：它找到下一个非空 Bucket，读取右侧文件并调用 `addBlockToJoinImpl()` 重建内存表，`onBuildPhaseFinish()` 后返回 `DelayedBlocks`；后者读取左侧文件、必要时再次按最新 Bucket 数 Scatter，再调用普通 `HashJoin` Probe。于是 Grace 层只负责文件和 Bucket 生命周期，Join Kind/Strictness 语义继续由内层 `HashJoinMethods` 保证。

## 12. DuckDB：让分区选择服从内存预留

DuckDB 的 [`physical_hash_join.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/physical_hash_join.cpp) 与 [`join_hashtable.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/join_hashtable.cpp) 把内存 Hash Join 与 External Hash Join 组织在同一套 `JoinHashTable` 中。

### 12.1 架构路径：Sink、Finalize、Operator 与 Source

DuckDB 的 Hash Join 同时实现 Pipeline Sink、Operator 和 Source 三种角色：

```text
Build child
  -> PhysicalHashJoin::Sink()          每线程 Local JoinHashTable
  -> PhysicalHashJoin::Combine()       收集 local_hash_tables
  -> PrepareFinalize()                 估算总工作集和最坏分区
  -> PhysicalHashJoin::Finalize()
       ├─ In-memory: merge -> unpartition -> build pointer table
       └─ External : repartition -> choose current partitions -> build pointer table

Probe child
  -> ExecuteInternal()
       ├─ PerfectHash probe
       ├─ normal JoinHashTable::Probe
       └─ external ProbeAndSpill

Source phase
  -> 输出 Right/Full 未匹配 Build Row
  -> External 模式继续 BUILD -> PROBE -> SCAN_HT 多轮循环
```

为什么 External Join 还需要 Source？第一轮 Probe 来自上游 Pipeline，可以直接走 `ExecuteInternal()`；没有进入当前 Build 分区的 Probe Row 已写入 `ProbeSpill`。之后上游输入已经结束，剩余分区需要由 Hash Join 自己驱动“重建表—读 Probe Spill—输出”，因此转为 Source 状态机。

Build 的线程本地路径也不是立刻并发写同一 Pointer Table：

```cpp
// 简化自 PhysicalHashJoin::Sink/Combine
Sink(chunk) {
    join_key_executor.Execute(chunk, join_keys);
    payload_chunk.ReferenceColumns(chunk, payload_columns);
    local_hash_table.Build(append_state, join_keys, payload_chunk);
}

Combine() {
    local_hash_table.GetSinkCollection().FlushAppendState();
    global.local_hash_tables.push_back(move(local_hash_table));
}
```

线程只向本地 `RadixPartitionedTupleData` 追加；到 Finalize 才决定合并、重分区和并行建立全局 Pointer Table。这把高并发的“数据物化”与更容易产生原子冲突的“索引构建”分开了。

### 12.2 Tuple Layout：Hash 在 Finalize 后变成 Next Pointer

`JoinHashTable` 构造时创建统一 [`TupleDataLayout`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/join_hashtable.cpp#L66)：

```text
Tuple row
┌──────────────────────┐
│ equality conditions  │  完整 Key，最终相等比较
├──────────────────────┤
│ residual columns     │  非等值 ON 条件需要的 Build 列
├──────────────────────┤
│ output payload       │  查询真正输出的 Build 列
├──────────────────────┤
│ optional found flag  │  Right/Full 类 Join 的逐行命中标记
├──────────────────────┤
│ hash / next pointer  │  Build 暂存 Hash；Finalize 后改作重复链 Next
└──────────────────────┘
```

`Build()` 先计算 Key Hash，并把它连同 Key/Payload 一起追加到 `sink_collection`。此时 Tuple 最后一槽保存 Hash，方便 Radix Partition 和 Finalize，尚不存在地址相关状态。

Finalize 遍历已经 Pin 住的 Tuple Block，读取最后一槽的 Hash，把 Tuple 地址插入 Pointer Table；若同 Key 已存在，则最后一槽被覆盖为旧链头地址，成为 Next Pointer。`pointer_offset` 同时标识这两个阶段的复用位置。

这种布局有两个重要结果：

1. Spill/移动阶段保存的是 Hash，Tuple 仍可安全重定位；
2. 一旦 Finalize 建立指针链，当前 `data_collection` 必须保持 Pin 住，直到本轮 Probe 完成。

### 12.3 Pointer Table：开放寻址与重复链是两套机制

Pointer Table Entry `ht_entry_t` 保存 Tuple Pointer 和 Hash Salt。插入时先做：

```text
offset = hash & bitmask
salt   = extract_high_hash_bits(hash)
```

随后进入开放寻址循环：

```cpp
// 结构化自 InsertHashesLoop()
while (entry[offset] occupied && entry[offset].salt != salt)
    offset = (offset + 1) & bitmask;

if (entry empty) {
    entry[offset] = {salt, tuple_ptr};
} else {
    candidate_ptr = entry[offset].pointer;
    gather candidate full key;

    if (full_key_equal) {
        tuple.next = candidate_ptr;       // 相同 Key：接入重复链
        entry[offset].pointer = tuple_ptr;
    } else {
        offset = next_offset;             // 只是 Salt/Hash 碰撞：继续线性探测
    }
}
```

所以要区分：

- **不同 Key 的 Hash 冲突**：Pointer Table 中线性探测；
- **相同 Key 的重复行**：Entry 指向链头，Tuple 内 Next Pointer 串链。

Salt 是一次廉价预过滤。Salt 不同就不需要访问随机 Tuple；Salt 相同才 Gather 完整 Key 并调用 `RowMatcher`。表较小时 Salt 收益不够，`UseSalt()` 可以关闭它。

并行 Finalize 使用 Atomic Entry：抢占空槽时 CAS；把重复行接到已有链头时用 Compare-Exchange 更新 Entry，同时把旧指针写进新 Tuple 的 Next。`chains_longer_than_one` 记录是否真的出现重复，Probe 可以为 Unique Key 走无链 Fast Path。

### 12.4 Probe：先定位指针，再由 ScanStructure 实现语义

`JoinHashTable::Probe()` 只完成 Key 准备、Hash 和候选 Pointer 定位：

```text
PrepareKeys(NULL/filter)
  -> Hash(keys)
  -> GetRowPointers(open addressing + salt + full-key compare)
  -> ScanStructure.pointers[]
```

真正的 Join Type 语义位于 `ScanStructure::Next()`：

```cpp
switch (join_type) {
case INNER/RIGHT:       NextInnerJoin(); break;
case LEFT/OUTER:        NextLeftJoin();  break;
case SEMI:              NextSemiJoin();  break;
case ANTI:              NextAntiJoin();  break;
case MARK:              NextMarkJoin();  break;
case RIGHT_SEMI/ANTI:   NextRightSemiOrAntiJoin(); break;
case SINGLE:            NextSingleJoin(); break;
}
```

对于 Inner Join，`ScanInnerJoin()` 对当前 Pointer 批量执行完整 Equality/Residual Predicate；产生结果后 `GatherResult()` 按 Pointer 从 `TupleDataCollection` 读取 Build 列。`AdvancePointers()` 再沿 Tuple Next Pointer 前进，直到所有重复链耗尽。

如果一个输入 Chunk 产生超过 `STANDARD_VECTOR_SIZE` 的结果，`last_match_count/last_sel_vector` 保存尚未输出的选择向量；下次 `ExecuteInternal()` 返回 `HAVE_MORE_OUTPUT`，继续消费同一输入 Chunk。这个设计把 Pipeline 的有限 Chunk 与 Join 的无限输出放大解耦。

Right/Full Join 的 `found` 位在匹配时被写为 `true`，上游 Probe 结束后再通过 Source 扫描 Build Tuple，输出未命中行。源码允许多个线程并发写同一个布尔位，因为唯一写入值都是 `true`，最终语义是幂等的。

### 12.5 Build 从一开始就保留 Radix 分区能力

Build Chunk 被组织成：

```text
[equality keys]
[non-equality condition columns]
[payload]
[optional found flag]
[hash]
[next pointer in tuple layout]
```

数据先进入 `RadixPartitionedTupleData`。Finalize 时，如果全部数据能够装入内存，就把分区合并并构建一张表；如果预算不足，则增加 Radix Bits，把数据保留为多个分区。

与“内存不足后再从哈希表反解行”相比，这种设计更早保存了可分区数据，但也要求 Build 数据布局从开始就兼顾内存与外存路径。

### 12.6 External 决策同时考虑 Tuple 与 Pointer Table

DuckDB 估算每个分区时，不只计算 Tuple 数据，还加上按行数估算的 Pointer Table：

```text
partition_ht_size = partition_data_size
                  + PointerTableSize(partition_count)
```

如果最大分区超过 `max_ht_size`，`SetRepartitionRadixBits()` 会增加分区位，并把目标估算到预算的一部分，为 Hash Table 构建和 Probe 留出余量。

这一点非常重要：只让逻辑行“勉强塞满”内存，会导致 Pointer Table 一分配就 OOM。可靠的 External Join 必须对恢复阶段的完整工作集做预算。

### 12.7 Finalize：由内存 Reservation 决定执行形态

`PrepareFinalize()` 先计算：

```text
total_size             = 所有 Tuple + 全局 Pointer Table
max_partition_ht_size  = 最大分区 Tuple + 该分区 Pointer Table
probe_side_requirement = Probe Radix Partition 的线程 × 分区 Buffer
```

然后向 `TemporaryMemoryManager` 注册最小 Reservation：至少要能处理一个最大 Build Partition，并为 Probe 分区 Buffer 留空间。`Finalize()` 的核心判断是：

```cpp
external = reservation < total_size;

if (external) {
    load_factor = EXTERNAL_LOAD_FACTOR; // 更紧凑的 Pointer Table
    if (max_partition_ht_size + probe_requirement > reservation)
        SetRepartitionRadixBits(...);
    PrepareExternalFinalize(reservation - probe_requirement);
} else {
    merge_local_hts();
    Unpartition();
    try PerfectHash;
    otherwise ScheduleFinalize();
}
```

External 模式将 Load Factor 从默认值调整为更紧凑的配置，有时仅靠减少 Pointer Table Capacity 就能重新落回 In-Memory 路径。若仍超限，新增 Radix Bits 的目标不是刚好低于预算，而是把估算最大分区压到约 `max_ht_size / 4`，为偏差、并行和中间 Buffer 留余量。

如果单个分区已占总大小 80% 以上，源码判断为 `very_very_skewed`，不再假设增加 Radix Bits 能有效均分。这是把“相同热点 Key 无法被 Hash 位拆开”直接编码进执行决策。

### 12.8 每轮选择一组能装下的分区

`PrepareExternalFinalize()` 会把未完成分区按大小近似排序，再选择一组总计不超过本轮预算的分区，移动到当前 `data_collection` 并构建 Pointer Table。

Probe 时，`ProbeAndSpill()` 根据 `current_partitions` 把输入分成两类：

- 当前已 Build 的分区立即 Probe；
- 其他分区写入 `ProbeSpill`，等待后续轮次。

每轮结束后释放当前数据，再选择下一组 Build Partition。这个过程与 StarRocks 的“多分区装箱”相似，但 DuckDB 明确把 `TemporaryMemoryState` 的 Reservation、Probe Side Requirement 和 Pointer Table 一并纳入选择。

External Source 的全局状态机更完整地表示为：

```text
INIT
  -> BUILD current_partitions
  -> PROBE matching ProbeSpill partitions
  -> SCAN_HT (仅 Right/Full 等需传播 Build 侧的 Join)
  -> Reset current HT
  -> BUILD next_partitions
  -> ...
  -> DONE
```

`PrepareExternalFinalize()` 会优先按近似大小从小到大选择分区，但对小差异做取整，尽量保留 Partition ID 顺序以降低 Eviction/I/O 抖动；并且即使单个分区超预算也至少选择一个，保证状态机能前进或暴露真实倾斜问题，而不是空转。

### 12.9 倾斜会反向限制并行度

DuckDB 在 Finalize/External Build 阶段检测 Key 是否倾斜。严重倾斜时，它可能改用单线程构建，避免多个线程同时争用同一冲突链或原子 Bucket。

这说明并行度不是越高越好：当 Key 分布高度集中时，增加线程既不能增加有效分区，也会放大同步和 Cache Coherence 成本。

## 13. Direct Mapping 与 Perfect Hash Join

如果单整数 Key 的值域很小且足够稠密，通用哈希表并非最佳结构：

```text
slot = key - min_key
```

这可以消除 Hash、碰撞与大部分 Key 比较。StarRocks 的 Range/Dense Range Direct Mapping 和 DuckDB 的 Perfect Hash Join 都在利用这个事实。

DuckDB 的 [`PerfectHashJoinExecutor`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/perfect_hash_join_executor.cpp) 会检查：

- 只有一个 Join 条件；
- Key 是整数类型且有 Min/Max 统计；
- 值域跨度低于阈值；
- Build Key 不存在无法表达的重复关系。

满足条件后，Build Payload 可以按值域直接放入数组，Probe 只需范围判断和偏移寻址。

这类优化的适用边界也很明确：

- 值域很大但数据稀疏时，数组空间会严重浪费；
- Key 重复时，一个 Slot 仍需表示多行；
- 统计错误可能导致原本节省内存的方案反而扩大内存。

因此直接映射不是“更快的通用 Hash Join”，而是基于值域证明成立后的专用物理算子。

## 14. Runtime Filter：在 Probe 之前减少工作

Hash Join 的 Build Key 集合天然可以生成过滤器：

- Min/Max Filter：适合有序或范围集中的数值；
- IN Filter：适合基数很小的 Build Key；
- Bloom Filter：适合较大集合，允许假阳性；
- Prefix/Range Filter：适合部分 String 或范围谓词。

Runtime Filter 的收益不只是减少 Probe 哈希查找。如果它能够下推到 Scan，还会减少解码、谓词计算、网络传输和上游 Exchange。

但 Filter 有三个约束：

1. **完整性。** 过滤器不能产生假阴性，否则会丢失正确结果；
2. **时机。** 等 Build 全部完成再发布，Probe 可能已经读完大量数据；
3. **Join 语义。** Outer/Anti/Mark Join 并不都允许用同样方式过滤 Probe 行。

Spill 又增加了第四个约束：Build Key 分散在文件中，系统要么维护可增量合并的全局摘要，要么只做分区级 Filter，要么放弃提前发布。

## 15. 四个引擎的实现对照

| 维度 | StarRocks | Apache Doris | ClickHouse | DuckDB |
| --- | --- | --- | --- | --- |
| 控制对象 | `HashJoiner` 四阶段 Controller | Local State + `ProcessHashTable*<JoinOp>` | `IJoin` 策略对象 | `PhysicalHashJoin` Sink/Operator/Source |
| Build 数据所有权 | 列式 `build_chunk` | Build `Block` | `ScatteredColumnsList` | `TupleDataCollection` |
| Bucket 组织 | Map Method 决定 Direct/Linear/Bucket Chained | `first[] + next[]` 行号链 | Typed HashMap Cell -> `RowRef/List` | Pointer Table 开放寻址 |
| Hash 冲突校验 | 具体 `JoinHashMap` 比较完整 Key | 沿 `next[]` 比较 `build_keys` | Typed Map Key 比较 | Salt 预筛 + Gather 完整 Key |
| Key 特化 | 单键、定长序列化、直接/范围映射、多种 Hash Map | 模板化 Hash Table Context 与 Join Op | 数值、128/256 bit、String、Hashed | 统一 Tuple Layout，Hash Salt + 完整比较 |
| Build 并行 | Pipeline Builder 与分区恢复 | Local State + 内部 Hash Join | Concurrent Hash Join/Two-Level Map | Local HT/分区 Finalize Task |
| 重复 Key | Chained/Set 等专用 Map | Build Unique 与 Probe 模板 | `RowRef` / `RowRefList` | Tuple Next Pointer |
| Spill 触发 | 内存撤销后切换 `SPILL_ALL` | 内存撤销后切换 Partitioned Join | Grace Hash 超限后扩 Bucket | Temporary Memory Reservation 不足时 External |
| Build 落盘 | Hash Table 转 Chunk 后列式分区 | Block 分区到 Spill File | 当前 Bucket 留内存，其余临时流 | `RadixPartitionedTupleData` |
| Probe 落盘 | 复用 Build Partition ID | 与 Build 文件成对入队 | 左右临时文件按 Bucket 配对 | `ProbeSpill` 同 Radix Partition |
| 恢复调度 | 可一次选择多个适配预算的分区 | 显式 `(build, probe, level)` 队列 | 逐 Bucket 重建与 Probe | 每轮选择一组当前分区 |
| 超大分区 | Spiller 分区层级与预算选择 | 显式递归重分区，深度受限 | 动态倍增 Bucket，上限受控 | 增加 Radix Bits，按预算重分区 |
| 特别优化 | Runtime Filter、Direct Mapping | Runtime Filter、Build Unique | Parallel Hash、Two-Level Map | Perfect Hash、Join Filter Pushdown |

这张表不意味着某个实现绝对更好。不同设计反映的是不同运行环境：

- 分布式 MPP 引擎更关注 Workgroup、内存撤销、异步 I/O 与 Runtime Filter；
- ClickHouse 把算法选择显式暴露，更方便用户按负载取舍；
- 嵌入式 DuckDB 必须把单进程内的 Buffer Manager、临时内存与并行任务统一调度。

## 16. 数据倾斜：递归分区的边界

假设一个 Key `hot_key` 占 Build 侧 60% 数据。无论使用 Hash 的第几位：

```text
hash(hot_key) = constant
```

所有 `hot_key` 行仍会进入同一个子分区。增加分区数量只能拆开不同 Hash 的 Key，不能拆开同一个 Key。

因此递归分区面对倾斜时可能出现：

```text
level 0: partition 3 too large
  -> level 1: partition 3.7 still too large
      -> level 2: partition 3.7.2 still too large
          -> reach max depth / fail
```

可选的工程策略包括：

- 对热点 Key 使用单独路径；
- 在允许时选择另一侧 Build；
- 改用 Sort Merge Join；
- 对特定 Join 语义给数据加 Salt，并复制另一侧热点行；
- 对 Semi/Anti Join 只保留 Key 是否存在，消除重复 Payload；
- 通过统计与采样提前识别热点，而不是等 Spill 多层后才发现。

Salting 并不是免费方案。若给 Build 热点 Key 拆成多个 Salt，Probe 侧可能需要复制或使用相同拆分规则；对 Outer Join 还要避免重复输出未匹配行。它是一个新的 Join 算法设计，而不是简单修改 Hash Seed。

## 17. 如何评估一套 Hash Join 设计

阅读源码或设计新执行器时，可以沿以下问题检查。

### 17.1 数据结构

- 是否为单整数、定长组合键和变长键选择不同表示？
- Bucket 中存完整行、Row ID，还是指针？
- 重复 Key 是否会产生大量小对象和随机分配？
- Outer Join 的命中标记按 Key 还是按 Row 保存？

### 17.2 内存模型

- 内存估算是否包含 Bucket、Payload、Arena、重复链和碎片？
- 扩容峰值是否同时持有旧表与新表？
- 哪些内存可撤销，撤销需要多长时间？
- 当前分区恢复时是否为 Pointer Table 和 Probe Buffer 留出空间？

### 17.3 Spill 协议

- Build/Probe 是否使用完全一致的 Hash 与 Partition Metadata？
- Spill 文件保存逻辑数据还是不可恢复的地址？
- 是否保留内存分区以减少 I/O？
- 超大分区如何递归处理，最大深度和失败信息是什么？
- 查询取消时，异步 I/O、临时文件和内存状态能否及时回收？

### 17.4 Pipeline 与可观测性

- Build Barrier 前后如何保持线程忙碌？
- I/O、解压、重建 Hash Table 与 Probe 能否重叠？
- 是否能看到每层分区大小、倾斜度、Spill/Restore Bytes、重分区次数和 Build/Probe 耗时？
- Profile 能否区分“磁盘慢”“哈希表重建慢”和“输出爆炸”？

### 17.5 Benchmark 必须覆盖状态转换，而不只是内存快路径

只用均匀随机 Key、内存充足、Inner Join 和 `count(*)` 测试，会系统性高估一套 Hash Join 的成熟度。更有区分度的矩阵应覆盖：

| 维度 | 至少包含的用例 | 主要验证目标 |
| --- | --- | --- |
| Key | 单整数、定长组合、长字符串、NULL、碰撞输入 | Key Encoding、Hash 与比较开销 |
| 分布 | 均匀、Zipf、单热点、重复 Build Key | 倾斜、重复链与最大分区 |
| 语义 | Inner、Left/Full Outer、Semi/Anti、Null-Safe | 匹配标记、NULL 与未匹配输出 |
| 内存 | 充足、临界、持续撤销、远小于 Build | In-Memory → Spill → Restore 状态转换 |
| 输出 | 低命中、高命中、多对多爆炸 | Probe Continuation 与 Backpressure |
| 存储 | NVMe、网络盘、对象存储式高延迟 | Spill 粒度、压缩与 I/O 并发 |

每组实验都应同时报告吞吐、P50/P99、峰值内存、Hash Table Expansion、Spill/Restore Bytes、临时文件数、最大分区、递归深度、输出行数和取消清理时间。否则一个“更快”的实现可能只是多占内存、没有进入 Spill，或者把工作推迟到下游输出。

跨引擎比较还要固定语义与物理前提：相同 Join 顺序、Build Side、并行度、内存上限、输入编码、压缩方式和热/冷缓存状态。比较默认配置更接近产品体验；比较算法则必须控制这些变量，两者不能混为同一结论。

## 18. 排障与调优：应该看什么

Hash Join 慢或 OOM 时，不应只看总 Spill Bytes。建议按以下顺序分析：

1. **确认 Join 选择与 Build 方向。** 统计是否把大表误判成小表？Outer Join 是否限制了可选方向？
2. **看 Build Row、Distinct Key 与值域。** 行数大但 Key 很少，重复链可能比 Bucket 更贵；小整数稠密时应关注直接映射机会。
3. **看最大分区而不是平均分区。** 平均 200 MiB 不代表最大分区不会达到 8 GiB。
4. **看 Spill Level 与 Repartition 次数。** 层级增长通常比总写盘量更能说明倾斜。
5. **看 Probe 输入与 Join 输出。** Probe 行多但输出少，应强化 Runtime Filter；输出远大于输入，则是多对多放大。
6. **看恢复阶段的内存峰值。** Tuple 数据装得下，不代表 Pointer Table、解压 Buffer 和输出 Chunk 同时装得下。
7. **看 CPU 与 I/O 是否重叠。** 磁盘未打满且 CPU 也空闲，往往是状态机串行化或分区过碎。

调优动作应该对应根因：

| 现象 | 可能原因 | 优先动作 |
| --- | --- | --- |
| Build 直接 OOM | Build 方向错误、估算漏项 | 更新统计、交换 Build 侧、启用 Spill |
| Spill 文件很多且很小 | 初始分区过多 | 减少初始分区、批量写与合并恢复 |
| 单分区反复重分区 | 热点 Key 倾斜 | Skew 策略、换算法或热点专用路径 |
| Probe CPU 很高但命中少 | 缺少前置过滤 | Runtime Filter、Scan 下推 |
| 输出 Chunk 持续爆满 | 多对多重复放大 | 检查业务语义、预聚合或去重 |
| Restore 慢 | I/O/解压/重建表瓶颈 | 调整压缩、并发恢复与分区装箱 |

## 19. 进一步思考：好的 Hash Join 是资源自适应算子

从四套源码可以看到，Hash Join 的演进方向并不是不断优化某个 Hash 函数，而是把更多不确定性纳入运行时控制：

- Key 分布决定数据结构；
- Distinct Count 决定 Set、Map 与重复链；
- 值域决定 Direct/Perfect Hash；
- 实际内存决定是否 Spill、分多少区、一次恢复几个分区；
- 倾斜决定递归是否有效、并行度是否应该降低；
- Join 语义决定哪些行可过滤、哪些匹配状态必须跨分区保存。

这也给优化器与执行器的边界提出了新要求。优化器不应只下发一个静态的 `HASH_JOIN` 节点，还应传递：

- Build 侧大小、NDV、Min/Max 与热点 Key 估计；
- Join Key 是否唯一；
- Runtime Filter 的候选类型；
- Spill 是否允许，以及资源预算；
- 可接受的退化算法与失败边界。

执行器则应把实际分区大小、重分区层级、峰值内存和命中率反馈出来。只有形成统计—执行—反馈闭环，Hash Join 才能从一个对估算敏感的算子，变成能够面对真实数据变化的资源自适应算子。

## 20. 源码阅读路线

如果希望继续深入，建议按以下顺序阅读。

### StarRocks

1. [`join_hash_table.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table.cpp)：Key Constructor 与 Hash Map Method 选择；
2. [`hash_joiner.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_joiner.cpp)：Build/Probe 状态封装；
3. [`spillable_hash_join_build_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_build_operator.cpp)：内存撤销与 Build 分区；
4. [`spillable_hash_join_probe_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_probe_operator.cpp)：分区选择、恢复与 Probe Spill。

### Apache Doris

1. [`join_hash_table.h`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/common/hash_table/join_hash_table.h)：Join Hash Table 结构；
2. [`hashjoin_build_sink.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_build_sink.cpp)：普通 Build 与 Runtime Filter；
3. [`hashjoin_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_probe_operator.cpp)：Join Type 模板与向量化 Probe；
4. [`partitioned_hash_join_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_probe_operator.cpp)：恢复队列与递归重分区。

### ClickHouse

1. [`HashJoin.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.cpp)：Key Method、Build 与 Probe；
2. [`ConcurrentHashJoin.h`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/ConcurrentHashJoin.h)：Two-Level Map 并行构建；
3. [`GraceHashJoin.h`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/GraceHashJoin.h)：外存 Join 的三阶段模型；
4. [`GraceHashJoin.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/GraceHashJoin.cpp)：Bucket 文件、动态 Rehash 与延迟 Probe。

### DuckDB

1. [`join_hashtable.hpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/include/duckdb/execution/join_hashtable.hpp)：Tuple Layout、Probe State 与 Spill State；
2. [`join_hashtable.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/join_hashtable.cpp)：Pointer Table、Salt、重复链与外存分区；
3. [`physical_hash_join.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/physical_hash_join.cpp)：Pipeline、Finalize Task 与临时内存预留；
4. [`perfect_hash_join_executor.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/perfect_hash_join_executor.cpp)：稠密整数值域的直接寻址。

## 21. 总结

Hash Join 的教科书模型只有 Build 和 Probe，工业实现却至少包含四层：

```text
SQL semantics
  └─ Key encoding and hash-table layout
      └─ vectorized build/probe and parallel scheduling
          └─ memory revocation, partition spill and recovery
```

StarRocks 展示了 MPP Pipeline 中可撤销内存、列式 Spill 与多分区恢复的结合；Doris 把递归重分区、深度限制和工作队列明确建模；ClickHouse 用 Hash、Parallel Hash 和 Grace Hash 呈现不同资源假设；DuckDB 则把 Tuple Layout、Radix Partition 和 Temporary Memory Reservation 统一到单机执行器中。

四种实现最终指向同一个原则：

> 一张快的哈希表只能解决内存充足时的问题；一套可靠的 Hash Join，还必须解释内存不足时数据如何分开、状态如何恢复、语义如何保持，以及倾斜无法再分时系统如何有边界地退化。

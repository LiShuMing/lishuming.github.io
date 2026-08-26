---
title: "【源码】深入 Spark Hash Join：从 JoinSelection、HashedRelation 到 AQE 与 Spill 边界"
date: 2026-08-25T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
slug: "dive-spark-hash-join"
categories:
  - 大数据
  - 数据库
tags:
  - Apache Spark
  - Hash Join
  - Sort Merge Join
  - AQE
  - Spill
  - Velox
description: "结合 Apache Spark 源码，分析 BroadcastHashJoin、ShuffledHashJoin、SortMergeJoin 的选择规则、执行路径、HashedRelation 内存模型、AQE 动态切换及 Native Engine 的 Spill 设计。"
draft: false
---

## 1. 背景：为什么 Spark 没有默认选择 Hash Join

在 StarRocks、ClickHouse 等分析型数据库中，Hash Join 通常是等值连接的核心实现：选择较小一侧构建哈希表，另一侧流式 Probe，在内存充足时可以获得接近 `O(N + M)` 的执行复杂度。

Spark SQL 的默认选择却不同。小表满足广播条件时，Spark 会优先使用 `BroadcastHashJoin`；一旦两侧都不能广播，常见执行计划往往变成 `SortMergeJoin`，而不是 `ShuffledHashJoin`。

这很容易被简化成“Spark 的 Hash Join 性能不好”，但源码展示的是一个更完整的取舍：

1. Spark 面向共享集群上的通用 ETL，数据规模和统计信息经常不可靠；
2. `ShuffledHashJoin` 要求每个 Task 的 Build 分区完整驻留内存；
3. Spark 的 `HashedRelation` 没有真正可用的 Join Spill 路径；
4. `SortMergeJoin` 的排序和同 Key 缓冲都可以落盘，失败边界更可控；
5. AQE 可以用真实分区大小重新选择 Join，但不会改变 Hash Table 本身不可 Spill 的事实。

因此，Spark 的策略并不是简单比较 Hash 与 Sort 的 CPU 复杂度，而是在性能、内存上界、数据倾斜和失败恢复之间做系统级决策。

本文基于 Apache Spark 提交 [`786bb3d`](https://github.com/apache/spark/tree/786bb3d9751fc6c4993997c088345ceba1b7a8d5) 分析以下问题：

- Spark 原生支持哪些 Join 算子？
- `JoinSelection` 如何在 BHJ、SHJ、SMJ 之间做静态选择？
- Broadcast 与 Shuffle Hash Join 的物理执行路径有什么区别？
- `HashedRelation` 为什么不能像 Sort 一样稳定 Spill？
- AQE 如何在运行时升级或降级 Join？
- Native Engine 为什么更倾向实现可 Spill 的 Partitioned Hash Join？

这里的配置默认值和 Rule 顺序只对应这一源码快照。Spark 官方当前的[配置文档](https://spark.apache.org/docs/latest/configuration)仍列出 `spark.sql.shuffledHashJoinFactor=3`，但具体发行版、Vendor Runtime 和 Native Plugin 可能改写 JoinSelection 或替换物理算子。排查生产计划时，应同时记录 Spark Build Version、Session Conf、Initial/Final Plan 和插件版本，不能仅凭文章中的默认值判断。

## 2. 先说结论

Spark Join 的核心判断可以浓缩成三层：

```text
第一层：能否 Broadcast？
  └─ 能：BroadcastHashJoin，避免两侧 Shuffle

第二层：每个 Shuffle 分区的 Build 侧是否足够小？
  └─ 能证明足够小：ShuffledHashJoin

第三层：无法证明 Hash Table 一定装得下
  └─ Join Key 可排序：SortMergeJoin
```

源码进一步给出六个关键结论：

1. **BHJ 是 Spark 等值 Join 的第一选择。** 默认自动广播阈值为 10 MiB，也可以由 Hint 或 AQE 的运行时统计触发。
2. **SHJ 默认很难被静态选中。** `spark.sql.join.preferSortMergeJoin=true`，即使关闭它，小表仍需同时满足“单分区可建 Hash Table”和“明显小于另一侧”。
3. **SHJ 与 BHJ 共用 `HashedRelation`。** 区别主要在 Build 输入的分发方式：前者两侧按 Key Shuffle，后者由 Driver 收集并广播 Build 侧。
4. **Spark Hash Join 的 Build 侧不支持稳定 Spill。** `UnsafeHashedRelation` 的 `BytesToBytesMap` 和 `LongHashedRelation` 在 Join 路径都无法释放有效内存，申请失败后 Task 直接失败。
5. **SMJ 的默认地位首先来自可预测性。** Shuffle Sort、External Sort Run 和重复 Key 缓冲都具备落盘路径，更适合大表 Join 和统计误差较大的 ETL。
6. **AQE 是重新规划，不是算子原地变形。** Stage 物化后，Spark 更新逻辑计划的运行时统计或注入 Hint，再重新执行 `JoinSelection`。

最重要的判断是：

> Spark 不是放弃 Hash Join，而是只在能够证明 Build 侧足够小时主动使用 Hash Join；当这个证明不足时，选择可 Spill 的 SortMergeJoin 作为安全基线。

## 3. Spark Join 的算子坐标

Spark SQL 的常见物理 Join 可以分为以下几类：

| 物理算子 | 前提 | 数据移动 | 主要内存对象 | Spill 能力 |
| --- | --- | --- | --- | --- |
| `BroadcastHashJoinExec` | 等值 Key、Build 侧可广播 | Build 侧广播；Stream 侧通常不 Shuffle | 全局广播 `HashedRelation` | Hash Table 不 Spill |
| `ShuffledHashJoinExec` | 等值 Key、分区 Build 侧足够小 | 两侧按 Key Shuffle | 每个 Task 一张 `HashedRelation` | Hash Table 不 Spill |
| `SortMergeJoinExec` | Join Key 可排序 | 两侧按 Key Shuffle 并排序 | Sort Buffer、相同 Key Row Buffer | 支持 Spill |
| `BroadcastNestedLoopJoinExec` | 任意条件 | 一侧广播 | 广播侧完整集合 | 高风险兜底 |
| `CartesianProductExec` | Inner-like 非等值连接 | 取决于输入 | 分区笛卡尔积 | 结果可能爆炸 |

对于等值 Join，BHJ、SHJ 和 SMJ 并不是三个完全独立的实现：

- BHJ 与 SHJ 共享 [`HashJoin`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashJoin.scala) 中的 Probe、Join Type 和 WholeStage Codegen 逻辑；
- BHJ 与 SHJ 都使用 [`HashedRelation`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashedRelation.scala)；
- SHJ 与 SMJ 都继承 [`ShuffledJoin`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/ShuffledJoin.scala)，要求两侧满足 Join Key 的聚簇分布。

这种分层很重要：选择 BHJ 还是 SHJ，首先改变的是数据如何到达 Build/Probe 算子；选择 Hash 还是 Merge，才真正改变分区内部的 Join 算法。

## 4. 静态规划：JoinSelection 的决策树

物理选择入口位于 [`SparkStrategies.JoinSelection`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/SparkStrategies.scala#L181)。公共判断逻辑位于 [`JoinSelectionHelper`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/optimizer/joins.scala#L290)。

### 4.1 先区分等值与非等值 Join

`ExtractEquiJoinKeys` 把 Join 条件拆成：

- 左右等值 Key；
- 不能放入 Key 的剩余条件 `nonEquiCond`；
- Join Type、左右子计划与 Hint。

BHJ、SHJ 和 SMJ 都依赖等值 Key。剩余条件可以在匹配后继续过滤，但如果完全没有等值 Key，Hash Join 和 SortMergeJoin 都不能直接使用，规划器会转向 Cartesian Product 或 Broadcast Nested Loop Join。

```text
Join condition
   │
   ├─ 有 equi keys ──► BHJ / SHJ / SMJ
   │                    └─ residual condition 在匹配后过滤
   │
   └─ 无 equi keys ──► Cartesian / Broadcast Nested Loop
```

### 4.2 Hint 的优先级

当用户提供 Hint 时，`JoinSelection` 按以下顺序尝试：

1. `BROADCAST`；
2. `SHUFFLE_MERGE`；
3. `SHUFFLE_HASH`；
4. `SHUFFLE_REPLICATE_NL`。

Hint 不是无条件命令。比如 Full Outer Join 不能规划为 BHJ，Hash Key 不满足二进制相等语义时也不能强制使用 Hash Join。无法应用的 Hint 会报告告警，并继续尝试其他策略或无 Hint 路径。

### 4.3 无 Hint 的默认顺序

`createJoinWithoutHint()` 的源码顺序非常直接：

```scala
createBroadcastHashJoin(false)
  .orElse(createShuffleHashJoin(false))
  .orElse(createSortMergeJoin())
  .orElse(createCartesianProduct())
  .getOrElse(createBroadcastNestedLoopJoin())
```

对应准入条件如下：

| 顺序 | 策略 | 关键条件 |
| --- | --- | --- |
| 1 | BHJ | Hash Key 支持、Join Type 允许、某侧大小不超过广播阈值 |
| 2 | SHJ | Hash Key 支持、Join Type 允许、关闭 prefer-SMJ、Build 侧分区可控且明显更小 |
| 3 | SMJ | Join Type 支持且 Key 可排序 |
| 4 | Cartesian | Inner-like Join |
| 5 | BNLJ | 最终兜底，源码明确提示可能很慢或 OOM |

所以，“Spark 默认选择 SMJ”并不完全准确。更精确的说法是：

> Spark 首先尝试 BHJ；对不能广播的大表等值 Join，SHJ 的准入条件默认关闭，于是 SMJ 成为最常见落点。

## 5. Hash Join 的三个准入条件

### 5.1 Key 必须支持二进制稳定相等

当前源码的 `hashJoinSupported()` 要求所有 Join Key 都满足：

```scala
UnsafeRowUtils.isBinaryStable(e.dataType)
```

这意味着 `UnsafeRow` 的二进制相等必须与 SQL 语义相等一致。当前实现重点排除包含非 Binary Collation String 的类型：两个字符串可能在大小写不敏感或重音不敏感规则下语义相等，但底层字节不同。

这里不能简单理解为“所有可 Hash 的 JVM 类型都能 Join”。Spark 要求 Hash、Equality 和 SQL Collation 语义闭合；不满足时会记录 Warning，并放弃 BHJ/SHJ。

### 5.2 Join Type 决定哪一侧可以 Build

Build 侧不是永远选右表。外连接必须保留特定一侧的未匹配行，因此不同 Join Type 的合法 Build 方向不同。

| Join Type | BHJ 常见合法 Build 侧 | SHJ 合法性特点 |
| --- | --- | --- |
| Inner / Cross Equi | 左或右，选统计更小侧 | 左或右 |
| Left Outer | 右侧 | SHJ 也支持 Build Left，但需额外扫描 Hash Table |
| Right Outer | 左侧 | SHJ 也支持 Build Right |
| Full Outer | 不支持 BHJ | SHJ 支持，需追踪 Build 侧匹配状态 |
| Left Semi / Left Anti | 右侧 | 通常 Build Right |

当两侧都合法时，`getBuildSide()` 根据 `stats.sizeInBytes` 选择更小的一侧。统计不准确不仅影响是否广播，也会影响哪一侧承担 Hash Table 内存。

### 5.3 大小判断不是一个阈值

BHJ 只需要满足 `canBroadcastBySize()`：

```text
0 <= sizeInBytes <= autoBroadcastJoinThreshold
```

当前源码默认 `spark.sql.autoBroadcastJoinThreshold=10MB`。

SHJ 静态选择则需要同时满足：

```text
preferSortMergeJoin == false

BuildSize < autoBroadcastJoinThreshold × numShufflePartitions

BuildSize × shuffledHashJoinFactor <= StreamSize
```

当前基线的 `spark.sql.shuffledHashJoinFactor` 默认值是 **3**。第二个条件用全局大小近似“平均每个 Shuffle 分区能装入与广播阈值相近的 Hash Table”；第三个条件要求 Build 侧明显小于 Stream 侧，以抵消构建 Hash Table 的成本。

这个估算有明显局限：平均值无法发现倾斜。总数据量满足条件，不代表最大的 Build 分区一定能放进单个 Task 的内存。

## 6. BroadcastHashJoin：用全局小表换掉 Shuffle

[`BroadcastHashJoinExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/BroadcastHashJoinExec.scala) 的核心价值不是 Hash Probe 本身，而是避免 Stream 侧 Shuffle。

```text
                  Driver
                    │
        collect Build side rows
                    │
             build HashedRelation
                    │
              Torrent Broadcast
          ┌─────────┼─────────┐
          ▼         ▼         ▼
      Executor 1 Executor 2 Executor N
          │         │         │
       probe local stream partitions
```

### 6.1 Build 侧在哪里构建

`BroadcastExchangeExec.relationFuture` 执行以下步骤：

1. `child.executeCollectIterator()` 把 Build 侧收集到 Driver；
2. `HashedRelationBroadcastMode.transform()` 构建 `HashedRelation`；
3. 检查行数上限和构建后字节数上限；
4. 通过 Spark Broadcast 发送序列化 Relation；
5. Executor Task 调用 `asReadOnlyCopy()`，以只读方式 Probe。

源码位于 [`BroadcastExchangeExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/exchange/BroadcastExchangeExec.scala#L121)。

BHJ 的内存风险因此横跨多个层次：

- Driver 需要收集并构建整个 Relation；
- Broadcast Block 需要网络传输和缓存；
- Executor 需要反序列化并持有只读 Hash Table；
- 多个并发 Broadcast 会竞争 Driver 与 Executor 内存。

当前源码还设置了执行期保护：

- `spark.sql.broadcastTimeout` 默认 300 秒；
- 普通 `UnsafeHashedRelation` 受 `BytesToBytesMap` 最大 Key 数约束；
- `spark.sql.maxBroadcastTableSize` 当前默认 8 GiB。

自动广播阈值与执行期上限不是一回事。Hint 可以绕过 10 MiB 的自动选择门槛，但不能让 Driver 内存和 Hash Table 容量变成无限。

### 6.2 Probe 与 WholeStage Codegen

非 Null-Aware Anti Join 路径的核心非常短：

```scala
val broadcastRelation = buildPlan.executeBroadcast[HashedRelation]()

streamedPlan.execute().mapPartitions { streamedIter =>
  val hashed = broadcastRelation.value.asReadOnlyCopy()
  join(streamedIter, hashed, numOutputRows)
}
```

`HashJoin` 根据 Join Type 生成不同路径：

- Inner Join：查到一个或多个 Build Row 后输出；
- Outer Join：没有命中时补 Null Row；
- Semi Join：首次满足条件后即可返回 Stream Row；
- Anti Join：确认无匹配后返回；
- Existence Join：输出是否存在匹配的标记。

若 `keyIsUnique=true`，代码生成可以调用 `getValue()`，避免为重复 Key 创建 Iterator；非唯一 Key 则调用 `get()` 遍历同 Key Value 链。唯一性因此不仅影响语义，还会改变生成代码的控制流和对象分配。

## 7. ShuffledHashJoin：每个分区独立 Build

[`ShuffledHashJoinExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/ShuffledHashJoinExec.scala) 要求两侧按 Join Key 形成相同的 `ClusteredDistribution`。`EnsureRequirements` 会在需要时插入 Shuffle Exchange。

```text
Left  ──Hash Shuffle──┐
                     ├─ Partition 0: build local hash + stream probe
Right ──Hash Shuffle──┘

                     ├─ Partition 1: build local hash + stream probe
                     └─ Partition N: build local hash + stream probe
```

### 7.1 一次 Task 的执行模型

`doExecute()` 使用 `zipPartitions` 配对两侧相同分区：

```scala
streamedPlan.execute().zipPartitions(buildPlan.execute()) {
  (streamIter, buildIter) =>
    val hashed = buildHashedRelation(buildIter)
    join(streamIter, hashed, numOutputRows)
}
```

这个流程存在明确的不对称：

- Build 分区必须被完整消费并形成 Hash Table；
- Stream 分区可以惰性读取，一行一行 Probe；
- Relation 在 Task Completion Listener 中关闭；
- 峰值内存由最大的 Build 分区决定，而不是平均分区大小决定。

所以 SHJ 的真正准入问题不是“总表是否足够小”，而是：

> 对每一个 Shuffle 分区，Build Rows、Hash Index、重复 Key 链和外连接匹配标记能否同时装入该 Task 可获得的 Execution Memory？

### 7.2 Outer Join 为什么更复杂

当 Full Outer Join，或者外侧恰好被选为 Build 侧时，只流式 Probe 无法输出未匹配的 Build Row。`buildSideOrFullOuterJoin()` 使用两阶段算法：

1. Probe Stream 侧，并记录哪些 Build Row 已匹配；
2. 遍历整个 `HashedRelation`，补出未匹配 Build Row。

Key 唯一时用 `BitSet` 记录 `keyIndex`；Key 不唯一时用 `OpenHashSet[Long]` 编码 `(keyIndex, valueIndex)`。这会增加额外内存，也会使这类 Join 无法保持普通 Stream 侧输出顺序。

### 7.3 Semi/Anti Join 的重复 Key 优化

对 Left Semi、Left Anti 等存在性语义，如果剩余条件不依赖 Build 侧非 Key 列，Build Hash Table 每个 Key 只需保留一行。

`ignoreDuplicatedKey` 会让 `HashedRelation` 在 Key 已存在时跳过后续 Value：

```text
普通 Inner Join：key -> row1 -> row2 -> row3
存在性 Join：    key -> one row
```

这一优化可以显著减少高重复维表的内存，但不能解决高基数 Build 分区无法装入内存的问题。

## 8. HashedRelation：Hash Join 的内存核心

`HashedRelation.apply()` 根据 Key 形态选择两种实现：

| Key 形态 | Relation | 底层结构 |
| --- | --- | --- |
| 单个 `LongType` 且不允许 Null Key | `LongHashedRelation` | `LongToUnsafeRowMap` |
| 通用 UnsafeRow Key | `UnsafeHashedRelation` | `BytesToBytesMap` |

### 8.1 UnsafeHashedRelation

`UnsafeHashedRelation` 使用 [`BytesToBytesMap`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/core/src/main/java/org/apache/spark/unsafe/map/BytesToBytesMap.java)：

- Append-only 开放寻址 Hash Table；
- Key/Value 以连续二进制记录写入 Memory Page；
- Hash Array 保存记录地址与 Hash Code；
- 同 Key 多 Value 通过地址链连接；
- 负载因子达到约 70% 时尝试扩容并 Rehash；
- 内存来自 `TaskMemoryManager`，可使用 On-Heap 或 Off-Heap Tungsten Memory。

逻辑布局可以简化为：

```text
Hash Array
┌──────────┬──────────┬──────────┐
│ addr/hash│ addr/hash│ addr/hash│
└────┬─────┴──────────┴────┬─────┘
     │                      │
     ▼                      ▼
Memory Pages
[key length][value length][key bytes][value bytes][next value address]
```

`UnsafeHashedRelation.apply()` 持续读取 Build Iterator，生成 Join Key 并调用 `loc.append()`。一旦 append 返回 false，代码释放 Map 并抛出 `cannotAcquireMemoryToBuildUnsafeHashedRelationError`。

### 8.2 LongHashedRelation

单 Long Key 可以走更专门的 `LongToUnsafeRowMap`：

- 初始使用 Sparse Open Addressing；
- Build 完成后 `optimize()` 尝试切换 Dense Mode；
- Dense Mode 通过 `key - minKey` 直接定位地址；
- 所有 UnsafeRow 字节紧凑放在 Page 中。

当 Key 范围紧凑时，Dense Mode 能减少 Hash 计算与冲突探测；当 Key 分布稀疏时则保留 Sparse Mode，避免按巨大数值范围分配数组。

## 9. Spill 真相：Spark Hash Join 为什么会直接失败

### 9.1 BytesToBytesMap 的 spill() 为什么帮不到 Join

`BytesToBytesMap` 是 `MemoryConsumer`，看上去实现了 `spill()`：

```java
public long spill(long size, MemoryConsumer trigger) throws IOException {
  if (trigger != this && destructiveIterator != null) {
    return destructiveIterator.spill(size);
  }
  return 0L;
}
```

关键是 `destructiveIterator`。它用于 `UnsafeKVExternalSorter` 接管 Map 内容的聚合/排序路径；Hash Join Build 不会建立这个迭代器。因此 Join 使用的 Map 被要求释放内存时，通常只能返回 `0L`。

`LongToUnsafeRowMap.spill()` 更直接：

```scala
def spill(size: Long, trigger: MemoryConsumer): Long = 0L
```

因此，Spark Hash Join 的内存行为是：

```text
申请 Hash Array / Memory Page
          │
          ├─ 成功：继续 Build
          │
          └─ 失败：无法把已有 Hash Partition 落盘
                     │
                     └─ append=false -> 释放 Relation -> Task 失败
```

### 9.2 统一内存管理不等于所有算子都可 Spill

`TaskMemoryManager` 可以在 Execution Memory 紧张时要求其他 `MemoryConsumer` 释放内存。Sort、Aggregation 或 Shuffle Buffer 可能把数据写盘，为 Hash Table 腾出空间；但 Hash Table 自己没有 Partitioned Spill 状态机。

这会形成一个不对称结果：

- Hash Join 可以从其他可 Spill 算子释放的内存中受益；
- 它自己无法有效响应内存回收；
- 如果同一 Task 中没有其他可驱逐对象，或者 Build 分区仍超过可用内存，Task 最终失败。

所以把 `spark.memory.fraction` 调大只能扩大失败边界，不能把 SHJ 变成 Grace Hash Join。

### 9.3 倾斜是 SHJ 最危险的输入

假设 Build 侧总大小 20 GiB、200 个分区，平均每个分区约 100 MiB，看起来可以接受；但如果一个热点 Key 形成 5 GiB 分区，该 Task 仍需一次性构建 5 GiB 以上的 Relation。

Hash Table 实际内存还高于原始输入：

```text
Build 原始 UnsafeRow
  + Join Key 副本
  + Hash Array 空槽
  + 地址与长度元数据
  + 重复 Key 链指针
  + Rehash 峰值
  + Outer Join 匹配标记
```

因此不能用 Shuffle 文件压缩后的字节数直接等价 Hash Table 峰值。

## 10. SortMergeJoin：为什么它是大表 Join 的安全基线

[`SortMergeJoinExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/SortMergeJoinExec.scala) 要求：

- 两侧按 Join Key 聚簇分区；
- 每个分区内部按 Join Key 升序排列。

```text
Left  -> Hash Shuffle -> External Sort ──┐
                                        ├─ Merge equal-key groups
Right -> Hash Shuffle -> External Sort ──┘
```

### 10.1 Merge 扫描并非完全 O(1) 内存

SMJ 同时向前扫描两个有序输入：

- Key 小的一侧继续前进；
- Key 相同时，缓存 Buffered Side 的同 Key Rows；
- Stream Side 相同 Key 的每一行与缓存集合做匹配。

如果某个 Key 有大量重复行，缓存集合仍可能很大。Spark 使用 `ExternalAppendOnlyUnsafeRowArray` 保存匹配行，并通过行数与字节阈值触发 Spill。

因此 SMJ 的优势不是“完全不使用内存”，而是：

- Sort Run 可以外排；
- 大型同 Key Buffer 可以外排；
- 内存需求不必等于整个 Build 分区；
- 最坏情况更多表现为磁盘 I/O 和长尾，而不是立即 OOM。

### 10.2 SMJ 仍然可能很慢

可 Spill 不等于无代价：

- 两侧 Shuffle 与排序有 CPU、网络和磁盘成本；
- 相同 Key 的多对多 Join 仍会产生笛卡尔式输出；
- 极端倾斜会造成单 Task 长尾；
- Spill 空间不足或本地盘性能差仍会导致失败或性能下降。

但对通用 ETL，磁盘退化通常比不可恢复的 Hash Build OOM 更容易治理。这正是源码配置 `preferSortMergeJoin=true` 的工程含义。

## 11. AQE：运行时重新选择 Join

AQE 不是在执行到一半时把 `SortMergeJoinExec` 对象直接改成 `BroadcastHashJoinExec`。其核心流程是：

```text
Query Stage 物化
      │
      ▼
获得真实 size / mapStats
      │
      ▼
把 Stage 包装回 LogicalQueryStage
      │
      ▼
invalidateStatsCache + AQEOptimizer
      │
      ▼
更新 Runtime Stats 或注入 Join Hint
      │
      ▼
重新执行 planner.plan()
      │
      ▼
再次进入 JoinSelection
```

入口位于 [`AdaptiveSparkPlanExec.reOptimize()`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/AdaptiveSparkPlanExec.scala#L793)。

### 11.1 Runtime Stats：SMJ/SHJ 升级为 BHJ

物化后的 `QueryStageExec` 产生 `isRuntime=true` 的统计。`canBroadcastBySize()` 检测到运行时统计后，会使用：

```text
spark.sql.adaptive.autoBroadcastJoinThreshold
```

未配置时回退到普通自动广播阈值。若某侧真实大小远小于静态估算，重新规划就可能从 SMJ/SHJ 变为 BHJ。

需要注意：如果 Shuffle Stage 已经物化，之前的 Shuffle 成本不会神奇消失。AQE 能避免后续 Sort/Merge，并可能用 Local Shuffle Read 读取已有 Block，但无法回收已经发生的网络和写盘成本。

### 11.2 DynamicJoinSelection：基于分区统计注入 Hint

[`DynamicJoinSelection`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/DynamicJoinSelection.scala) 读取 `ShuffleQueryStageExec.mapStats.bytesByPartitionId`，有两类核心判断。

#### 避免不划算的 Broadcast

如果某侧包含大量空分区，Shuffle Join 的许多 Task 可以快速短路；把另一侧广播后，反而可能失去这个优势。规则会根据 Join Type 和哪一侧为空，注入 `NO_BROADCAST_HASH`。

Outer/Anti Join 需要保留未匹配行，不能简单在任意空侧短路，因此源码对 `LeftOuter`、`RightOuter`、`LeftAnti` 做了额外区分。

#### SMJ 升级为 SHJ

如果所有分区都满足：

```text
partitionSize <= maxShuffledHashJoinLocalMapThreshold
```

并且 `advisoryPartitionSizeInBytes` 也不大于该阈值，规则会注入 `PREFER_SHUFFLE_HASH`。若同时需要禁用 Broadcast，则注入更强的 `SHUFFLE_HASH`。

这个条件的本质是：AQE 已经看到每个 Build 分区的真实字节数，可以比静态“总大小除以分区数”更可靠地判断局部 Hash Table 风险。

但当前基线中：

```text
spark.sql.adaptive.maxShuffledHashJoinLocalMapThreshold = 0
```

因此，除非用户显式配置，AQE 通常不会主动把 SMJ 升级为 SHJ。这再次说明 Spark 对无 Spill Hash Join 的默认态度非常保守。

### 11.3 用户 Hint 优先于 AQE Hint

`DynamicJoinSelection` 只在对应一侧没有用户 Join Strategy Hint 时写入 AQE Hint。优先级由此保持一致：

```text
用户显式 Hint
    > AQE 根据真实统计注入的 Hint
    > 静态自动选择
```

### 11.4 已经广播的 Stage 不允许回退

[`LogicalQueryStageStrategy`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/LogicalQueryStageStrategy.scala) 排在普通策略之前。

如果 Join 某一侧已经是 `BroadcastQueryStageExec`，规则会强制保留 BHJ，防止因 Stage 完成顺序或统计波动，把已经构建和广播的数据重新改成 Shuffle Join。

这是一种 Sunk Cost 保护：物理 Stage 一旦完成，重新规划必须尊重已经付出的执行成本。

### 11.5 Local Shuffle Read 的准确位置

[`OptimizeShuffleWithLocalRead`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/OptimizeShuffleWithLocalRead.scala) 常被误解为“BHJ 降级为 Shuffle Join 后使用 Local Read”。源码实际处理的是：

- AQE 重规划得到 BHJ；
- Probe 侧已经存在物化的 Shuffle Stage；
- 在不引入额外 Shuffle 的前提下，改用本地 Shuffle Block 读取。

它服务于 **Shuffle Join → BHJ** 的升级复用，而不是给 **BHJ → Shuffle Join** 的降级提供本地读。

## 12. 倾斜优化能否解决 SHJ OOM

[`OptimizeSkewedJoin`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/OptimizeSkewedJoin.scala) 可以识别显著大于中位数和绝对阈值的分区，将一侧切成多个 Mapper Range，并复制另一侧对应分区。

```text
原始：        (L-skew, R)

拆分后：      (L-1, R-copy-1)
              (L-2, R-copy-2)
              (L-3, R-copy-3)
```

它能降低单 Task 输入并增加并行度，但不能从根本上赋予 `HashedRelation` Spill 能力：

- Build 分区拆分后仍需完整驻留；
- 若倾斜来自单个超高频 Key，另一侧复制可能增加总 I/O；
- 两侧同时倾斜时，Split 组合可能形成更多 Task；
- Hash Table 放大系数仍未被 Map Output Bytes 精确表达。

因此，Skew Join Optimization 是风险缓解，不是 SHJ 内存安全证明。

## 13. Native Engine：为什么 Hash Join 更容易成为默认方案

现代 Native Engine 往往采用向量化 Hash Probe、显式内存池和 Partitioned Spill。以 Velox 为例，Hash Join 被拆成 `HashBuild`、`HashProbe` 与 `HashJoinBridge`：

```text
Build vectors -> HashBuild -> in-memory HashTable
                      │
                      ├─ memory arbitration / reclaim
                      │
                      └─ spill selected hash partitions
                                  │
Probe vectors -> HashProbe --------┤
                      │            │
                      └─ spill matching probe partitions
                                  │
                          restore partition and re-join
```

公开源码中的 [`HashBuild`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashBuild.h) 和 [`HashProbe`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashProbe.h) 都包含 Spill、Memory Reclaim 和递归恢复状态。

这与 Spark JVM `HashedRelation` 的关键区别不是 C++ 一定比 JVM 快，而是算法状态机不同：

- Spark SHJ：一个 Build 分区对应一张必须完整驻留的 Hash Table；
- Partitioned Hash Join：先按 Hash Bit 切分，内存不足时只保留部分 Partition，Build/Probe 对称落盘，随后逐 Partition 恢复。

Native Engine 还可以利用批量 Probe、SIMD、Dictionary Vector 和更精细的 Memory Arbitration 降低 CPU 与对象开销。但 Spill 一样会引入磁盘 I/O、递归分区、文件数量和长尾问题，并不是免费的性能保险。

对于 Databricks Photon、Snowflake 等闭源引擎，公开资料可以说明它们采用 Native/Vectorized Execution，但无法像本文分析 Spark 和 Velox 一样验证具体 Hash Table、Partition 数、Spill 触发和恢复协议。因此不应把产品层性能描述当成可复现的源码结论。

## 14. 如何选择与调优

### 14.1 优先修复统计，而不是先写 Hint

BHJ 是否被选择、Build Side 是哪一侧，都依赖 `sizeInBytes`。如果统计长期失真：

- 小表可能错过 Broadcast；
- 大表可能被 Hint 强制广播并压垮 Driver；
- SHJ 可能选错 Build Side；
- AQE 只能在 Stage 物化后补救，无法消除已经发生的 Shuffle。

应优先检查 Catalog Statistics、文件大小、过滤选择率和运行时 Plan，而不是把 Hint 当作永久修复。

还要注意，Planner 证明的是**估算输入字节满足启发式阈值**，不是“最终哈希表一定装得下”。源码中的 `canBuildLocalHashMapBySize()` 使用 `sizeInBytes < autoBroadcastJoinThreshold × numShufflePartitions` 近似判断单分区规模，`muchSmaller()` 再比较两侧总字节；它没有在静态规划阶段知道最大分区、重复 Key、Hash Bucket、UnsafeRow 对齐和并发 Task 的真实内存峰值。

因此一条可信的 SHJ 准入证据应至少包含：

```text
estimated build bytes
  + partition size distribution, especially max/P99
  + row width and duplicate-key distribution
  + hashed-relation expansion factor
  + concurrent tasks per executor
  + available execution memory after other consumers
```

平均分区很小但单个热点分区很大时，增加 Executor 总内存可能只会推迟失败。更有效的动作通常是提高有效分区数、治理热点 Key、交换 Build Side，或者保留可 Spill 的 SMJ。

### 14.2 何时适合 BHJ

- Build 侧经过过滤后确定很小；
- Driver 有足够内存完成 Collect 与 Build；
- Executor 能承受并发 Broadcast Relation；
- Stream 侧很大，避免 Shuffle 的收益明显；
- Join Type 允许相应 Build Side。

### 14.3 何时可以考虑 SHJ

- 两侧已经按 Join Key 分区，或者 Shuffle 无法避免；
- Build 侧显著小于 Stream 侧；
- AQE 真实统计证明所有 Build 分区都小；
- 数据倾斜已经治理；
- Task Execution Memory 有明确余量；
- 可以接受 Build OOM 时 Task 失败，而不是稳定 Spill。

不要仅因为 Hash Join 理论复杂度更低，就全局设置：

```text
spark.sql.join.preferSortMergeJoin=false
```

这会扩大 SHJ 候选范围，却不会增加 Spill 能力。

### 14.4 何时保留 SMJ

- 大表对大表；
- 数据规模与选择率难以准确估计；
- Build 侧可能倾斜；
- ETL 更重视稳定完成而非最低 CPU；
- 下游可以复用 Join Key Ordering；
- 本地磁盘 Spill 能力充足。

## 15. 排障时应该看什么

### 15.1 先看 Initial Plan 与 Final Plan

启用 AQE 后，单看初始 `EXPLAIN` 不足以判断实际 Join。需要对比：

```text
Initial Plan
  SortMergeJoin

Final Plan
  BroadcastHashJoin / ShuffledHashJoin / SortMergeJoin
```

如果计划没有按预期切换，应依次检查：

1. 是否是 Equi Join；
2. Key 是否 Binary Stable；
3. Join Type 是否允许目标 Build Side；
4. 用户 Hint 是否阻止 AQE 注入策略；
5. Runtime Stats 是否产生；
6. Adaptive Threshold 是否配置；
7. Broadcast Stage 是否已经物化并被锁定。

### 15.2 关键指标

| 算子 | 建议关注指标 |
| --- | --- |
| BroadcastExchange | `dataSize`、`numOutputRows`、`collectTime`、`buildTime`、`broadcastTime` |
| BroadcastHashJoin | 输出行数、Peak Execution Memory、Broadcast Timeout |
| ShuffledHashJoin | `buildDataSize`、`buildTime`、`numOutputRows`、Task Peak Memory、失败分区 |
| SortMergeJoin | `spillSize`、Sort Spill、Shuffle Read、同 Key Buffer 与长尾 Task |
| AQE | Initial/Final Plan、Partition Size 分布、Skew Partition、Local Shuffle Read |

对 SHJ，平均 `buildDataSize` 价值有限。真正应该观察的是最大分区、P95/P99 分区和失败 Task 对应的 Shuffle Partition。

### 15.3 常见误区

| 误区 | 更准确的理解 |
| --- | --- |
| Hash Join 一定比 SMJ 快 | 内存足够且分布稳定时通常更快；OOM 时没有完成时间 |
| 开启 Off-Heap 就能避免 OOM | 只改变内存位置，不增加 Join Spill 状态机 |
| AQE 会自动把所有 SMJ 变成 SHJ | SHJ 本地 Map 阈值默认是 0，需要显式配置 |
| Map Output 分区小就一定安全 | Hash Table 有索引、空槽、Key 副本和 Rehash 放大 |
| Skew Join 能彻底解决 Hash OOM | 它降低单分区大小，但 Build 仍必须驻留 |
| Broadcast Hint 可以忽略大小 | Hint 绕过自动阈值，不绕过 Driver/Executor 物理内存 |

## 16. 一张图串起完整决策

```text
Logical Join
    │
    ├─ 没有 Equi Key ───────────────► Cartesian / Broadcast Nested Loop
    │
    └─ 有 Equi Key
         │
         ├─ Key 非 Binary Stable ───► SortMergeJoin（若可排序）
         │
         └─ Hash Key 支持
              │
              ├─ User Hint 可应用 ──► 按 Hint 优先级选择
              │
              └─ 自动选择
                   │
                   ├─ 一侧可 Broadcast ─────────► BroadcastHashJoin
                   │
                   ├─ preferSMJ=false
                   │   + Build 可建局部 Map
                   │   + Build 明显更小 ────────► ShuffledHashJoin
                   │
                   ├─ Key 可排序 ───────────────► SortMergeJoin
                   │
                   └─ 最终兜底 ─────────────────► BNLJ
                                                │
                                      Stage 物化 / AQE
                                                │
            ┌───────────────────────────────────┼────────────────────────┐
            │                                   │                        │
      Runtime Size 变小                  所有分区足够小             大量空分区
            │                                   │                        │
      重新规划为 BHJ                 PREFER_SHUFFLE_HASH       NO_BROADCAST_HASH
            │                                   │                        │
      可复用 Local Read                      可能改为 SHJ           保留 Shuffle Join
```

## 17. 源码阅读路线

建议按“选择 → 分发 → 数据结构 → AQE”的顺序阅读：

1. [`SparkStrategies.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/SparkStrategies.scala#L181)：确认策略顺序；
2. [`joins.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/optimizer/joins.scala#L290)：理解 Build Side、大小与 Key 限制；
3. [`BroadcastExchangeExec.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/exchange/BroadcastExchangeExec.scala)：理解 Driver Collect/Build/Broadcast；
4. [`ShuffledHashJoinExec.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/ShuffledHashJoinExec.scala)：跟踪 Task Build 与 Outer Join；
5. [`HashJoin.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashJoin.scala)：理解 Probe 和 Codegen；
6. [`HashedRelation.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashedRelation.scala)：理解 Hash Table 与失败路径；
7. [`BytesToBytesMap.java`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/core/src/main/java/org/apache/spark/unsafe/map/BytesToBytesMap.java)：确认内存分配、扩容和 `spill()`；
8. [`SortMergeJoinExec.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/SortMergeJoinExec.scala)：对比 Merge Buffer 与 Spill；
9. [`DynamicJoinSelection.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/DynamicJoinSelection.scala)：理解运行时 Hint；
10. [`AdaptiveSparkPlanExec.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/AdaptiveSparkPlanExec.scala)：理解 Stage 物化与重新规划。

## 18. 总结

Spark Hash Join 的设计可以归纳为四层约束：

1. **语义约束**：必须有等值 Key，二进制 Hash/Equality 必须与 SQL 语义一致；
2. **分发约束**：BHJ 需要全局小表，SHJ 需要每个局部 Build 分区足够小；
3. **内存约束**：`HashedRelation` 必须完整驻留，Join 路径没有可靠 Spill；
4. **运行时约束**：AQE 可以修正统计和策略，但不能改变算子的基本内存模型。

这也解释了 Spark 与 OLAP/Native Engine 的差异：长期驻留的数据库引擎可以围绕 Hash Join 构建精细的内存仲裁、分区 Spill、Runtime Filter 和 Pipeline 调度；Spark JVM 执行器则选择将稳定性更强的 SMJ 作为大表 Join 基线，再用 Broadcast、Hint 和 AQE 有条件地切换到 Hash Join。

最终，Join 选择不是一道“Hash 还是 Sort”的静态算法题，而是一个端到端系统问题：

> 优化器必须证明数据分发与内存上界，Runtime 必须兑现这个证明；如果 Hash Table 没有退化路径，那么保守的计划往往不是性能不足，而是对失败成本的诚实定价。

## 参考资料

- [Apache Spark 源码，commit 786bb3d](https://github.com/apache/spark/tree/786bb3d9751fc6c4993997c088345ceba1b7a8d5)
- [Spark JoinSelection](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/SparkStrategies.scala#L181)
- [Spark HashedRelation](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashedRelation.scala)
- [Spark BytesToBytesMap](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/core/src/main/java/org/apache/spark/unsafe/map/BytesToBytesMap.java)
- [Spark Adaptive Query Execution](https://github.com/apache/spark/tree/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive)
- [Velox HashBuild](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashBuild.h)
- [Velox HashProbe](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashProbe.h)

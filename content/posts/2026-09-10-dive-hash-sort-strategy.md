---
title: "【调研】Hash 还是 Sort：从优化器决策到运行时自适应"
date: 2026-09-10T00:00:00+08:00
lastmod: 2026-09-18T00:00:00+08:00
slug: "dive-hash-sort-strategy"
categories:
  - 数据库
tags:
  - Hash Join
  - Sort-Merge Join
  - 查询优化器
  - 执行引擎
  - CBO
  - 自适应查询执行
description: "结合 CockroachDB、TiDB、OceanBase、Trino 与 Apache Doris 源码，系统分析优化器与执行器如何在 Hash、Sort、数据分布、物理属性、内存风险和运行时自适应之间做出选择，并沿经典论文到 2026 年研究梳理其演进路线。"
draft: false
---

## 摘要

“Hash 还是 Sort”看似是算法课里的复杂度比较，真正落到数据库系统，却同时牵涉四个层面：

- **物理算法**：Hash Join、Merge Join、Hash Aggregate、Stream Aggregate；
- **数据组织**：无序、局部有序、全局有序、Hash 分区、Range 分区；
- **资源行为**：CPU、缓存、内存峰值、磁盘 I/O、网络、并发和尾延迟；
- **不确定性处置**：统计误差、数据倾斜、内存波动、spill 与运行时改判。

因此，正确的问题不是“`O(N)` 是否一定小于 `O(N log N)`”，而是：

> 在当前物理属性、资源预算和估计可信度下，哪条执行路径的整体收益最高；如果判断错误，系统又能否把损失限制在可接受范围内？

对 CockroachDB、TiDB、OceanBase、Trino 与 Apache Doris 的源码分析显示，成熟系统并不存在一条统一路线：CockroachDB 把 Ordering 放入搜索状态，并在外部 Hash Join 递归分区失效时切换到外排加 Merge Join；TiDB 让 Merge Join 与 Stream Aggregate 依赖输入属性，同时让 Hash Join V2 在运行时按分区 spill 和恢复；OceanBase 把 Hash、Merge、Sort、Top-N、Prefix Sort 与 Group By 的成本拆得更细；Trino 和 Doris 则主动缩小 Join 算法集合，把工程资源集中到 build side、数据分布、Runtime Filter 和 spill。

这些实现共同指向一个结论：

> **优化器负责选择低预期成本的初始策略，执行器负责约束错误选择的最大 regret；Hash 与 Sort 的边界应当是一张随数据、属性、资源和硬件变化的 crossover surface，而不是一个固定阈值。**

---

## 1. 先拆掉一个伪命题：Hash 与 Sort 不是一对互斥算子

### 1.1 两种基础的数据重组方式

Hash 与 Sort 更接近两种“组织数据”的原语：

```text
Hash
  key -> hash value -> bucket / partition
  擅长：等值查找、分组、去重、重分区

Sort
  key -> total / prefix order -> run / range
  擅长：顺序归并、范围处理、顺序聚合、Top-N、Window
```

一个物理算子往往组合多个原语。Grace Hash Join 会先 Hash Partition，再对能放入内存的分区建立 Hash Table；外部 Sort 会生成多个有序 run，再做多路归并；分布式 Merge Join 之前还可能需要 Range Exchange；Hash Join 生成的 Runtime Filter 又能改变下游 Scan 的实际输入规模。

甚至同一个算子内部也可能从 Hash 过渡到 Sort。CockroachDB 的 External Hash Join 就是直接证据：它先递归 Hash Partition；如果继续换 Hash Seed 仍不能有效缩小分区，说明倾斜或重复键使递归失去进展，于是该分区改走 Disk-backed Sort + Merge Join。

```text
输入
 ├─ Hash Partition A
 │    ├─ 可装入内存的分区 -> In-memory Hash Join
 │    └─ 仍然过大
 │          ├─ Hash Partition B -> 继续递归
 │          └─ 分区不再有效缩小 -> Sort(left/right) -> Merge Join
 └─ 输出
```

这意味着“Hash 与 Sort 选择”至少包含三类不同决策：

1. **优化期算子选择**：Hash Join 还是 Merge Join，Hash Aggregate 还是 Stream Aggregate；
2. **优化期属性选择**：是否值得提前建立 Ordering/Distribution 并由后续多个算子复用；
3. **运行期退化路径选择**：继续分区、局部 spill、算法切换，还是在 stage 边界重新优化。

### 1.2 复杂度只描述了很小一部分事实

对一个等值 Join，常见的粗略模型是：

```text
C_hash_join
  = C_build
  + C_probe
  + C_exchange
  + E[C_spill]

C_merge_join
  = C_sort(left)
  + C_sort(right)
  + C_merge
  + C_exchange
  - B_order_reuse
```

其中：

```text
C_build ~= N_build * (hash + insert + materialize)
C_probe ~= N_probe * (hash + lookup + predicate)

C_sort(N) ~= N * log(N) * compare_cost + external_IO
C_merge ~= (N_left + N_right + N_matches) * sequential_cost
```

真正决定结果的不是大 O，而是常数、非线性拐点和可复用属性：

- Hash Table 是否进入 LLC，探测是连续访问还是随机访问；
- key、payload、pointer、load factor 和 allocator overhead 共同造成多大内存放大；
- build side 是否估反，是否存在大量重复键或 hot partition；
- 排序键能否使用 radix/key normalization/SIMD，而不是昂贵的通用 comparator；
- 输入是否已按 Join Key 有序，或者只需补齐一段前缀；
- 排序结果是否会被后续 Aggregate、Window、Merge Join 或最终 `ORDER BY` 复用；
- 分布式执行中，Broadcast、Hash Shuffle 与 Range Shuffle 谁主导成本；
- 内存不足后，是一次顺序 spill，还是多轮递归分区和随机 I/O。

所以，一个只看 `row_count` 的模型并没有在选择算法，它只是在选择一个经验常数。

### 1.3 “流式”也不能只贴算子标签

常见说法是 Hash 阻塞、Merge 流式。这个判断需要更精确：

| 算子 | 首行输出前的必要状态 | 关键边界 |
|---|---|---|
| Hash Join | 至少完成 build side | Probe 可以流水；Full/Outer Join 可能还需扫描未匹配 build rows |
| Merge Join | 两侧必须满足顺序 | 重复键组可能需要缓存；若上游要排序，整体仍然阻塞 |
| Hash Aggregate | 通常维护全部 group state | Partial/Streaming Pre-Aggregation 可以提前减量，但不等于最终结果可提前输出 |
| Stream Aggregate | 输入须按 group key 聚集 | 可以按组完成即输出，状态接近“当前 group” |
| Sort | 传统全排阻塞 | Top-N、Prefix Sort、Incremental Sort 的状态和启动成本明显不同 |

因此，`LIMIT 10`、交互式查询和全量 ETL 不应共享同一套单一权重。

---

## 2. 优化器实际要搜索的是三维空间

### 2.1 算法、移动与执行模式必须联合决定

一个 Join Candidate 不应只表示 `HASH_JOIN`，而应至少表达：

```text
JoinCandidate {
    algorithm: HASH | MERGE | LOOKUP | NESTED_LOOP
    build_side: LEFT | RIGHT | NONE
    distribution: COLOCATED | BROADCAST | HASH_SHUFFLE | RANGE_SHUFFLE
    ordering: required/provided order
    runtime_mode: IN_MEMORY | SPILLABLE | PARTITIONED | ADAPTIVE
}
```

原因很直接：

- Broadcast Hash Join 与 Partitioned Hash Join 的网络、内存和并发行为完全不同；
- Merge Join 若能复用 Scan Ordering，可能几乎没有额外 Sort Cost；若要两侧全局排序，代价又完全不同；
- 同一个 Hash Join，在“每个 worker 都复制 build side”和“build side 按 key 分区”下，峰值内存差一个集群规模因子；
- spillable 并不意味着 spill 免费，它只是把 OOM 变成了可能很贵但可完成的执行。

### 2.2 先做可行性过滤，再比较代价

Cost Model 不应该替代语义约束。候选生成至少要先回答：

```text
Merge Join:
  - 是否存在可归并的等值条件？
  - key 的类型、collation、NULL 语义是否支持？
  - 两侧能否提供兼容 Ordering？

Hash Join:
  - 是否存在可 Hash 的等值条件？
  - Join Type 是否允许当前 build/probe 方向？
  - 运行时是否有可用的内存与 spill 实现？

Distribution:
  - Join Type 是否允许复制一侧？
  - build bytes 是否在 broadcast 上限内？
  - child distribution 是否已满足 colocate？
```

TiDB 源码就是一个很清楚的例子：Merge Join 不只是“算一遍成本”。候选生成阶段会检查 join key、`NULL-safe equality`、左右已有属性、排序前缀与 collation；条件不成立时，它根本不进入竞争集合。

### 2.3 Physical Property 不是附属信息

局部最优容易破坏全局最优：

```text
Plan A: 局部 Hash 更便宜

HashJoin(k)
   -> HashAgg(k)
      -> Sort(k, ts)
         -> Window(k ORDER BY ts)

Plan B: 前面多维护一点顺序，后面连续复用

OrderedScan(k, ts)
   -> MergeJoin(k)
      -> StreamAgg(k)
         -> Window(k ORDER BY ts)
```

如果只给 Join 算子独立计价，Plan A 很容易胜出；如果 Memo 的状态包含 `required/provided ordering`，Plan B 才可能被正确保留。

Ordering 的价值也不能用一个固定 bonus 表示。它是一个可消费的物理属性：

```text
B_order_reuse
  = avoided_sort
  + avoided_shuffle_or_merge
  + lower_aggregation_state
  + lower_startup_latency
  - cost_to_preserve_order
```

同样的思路也适用于 Distribution：一个当前 Join 产生的 Hash Partitioning，可能让下一个 Join 或 Aggregate 免于再次 Shuffle。

---

## 3. 源码剖析

比较五个系统如何回答三个问题：

1. 哪些候选进入优化器？
2. 成本与物理属性如何参与选择？
3. 估计错误后由谁兜底？

### 3.1 CockroachDB：属性驱动候选，执行器允许 Hash 转 Sort

CockroachDB 的规则 `GenerateMergeJoins` 明确使用 interesting ordering 生成 Merge Join 候选。也就是说，Merge Join 不是扫描到等值谓词就机械生成，而是与可获得的顺序属性绑定。源码见 [GenerateMergeJoins](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/rules/join.opt#L253-L258)。

Cost Model 对两条路径的刻画也保留了执行实现特征。下面是 `computeHashJoinCost` 与 `computeMergeJoinCost` 的核心行，变量名为源码原名：

```go
// Hash Join：右侧进入 hash table，因此系数更高，并额外计入 buffering 与 spill 压力。
cost := memo.Cost{C: (1.25*leftRowCount + 1.75*rightRowCount) * cpuCostFactor}
cost.Add(c.rowBufferCost(rightRowCount))

// Merge Join：向量化实现下左侧更接近 streaming，右侧在部分场景缓存重复键组。
cost := memo.Cost{C: (0.9*leftRowCount + 1.1*rightRowCount) * cpuCostFactor}
```

两个函数还有一处容易被忽略的共同处理：当 Join 是 Semi 或 Anti 且 `leftRowCount < rightRowCount` 时，代码直接交换两侧行数。原因是执行引擎总是把 hash table 建在右侧，execbuilder 会把基数更小的一侧换到右边，而优化器没有表示 right semi/anti join 的表达式，只能在计价时模拟这个交换。这正是 §2.1 所说“build side 必须是候选维度”的一个具体代价：**成本函数若不知道执行器会怎么选边，同一棵计划树的代价就会算反。**

这不是一个精确的硬件模拟器，但至少把非对称 build side 和 memory pressure 放入了决策。源码见 [CockroachDB Hash/Merge Join cost](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/coster.go#L1062-L1136)。

更值得注意的是运行时的 External Hash Join：

```text
Phase 1: 两侧按 hash A 分区并落盘
Phase 2: 成对读取分区，以 hash B 做内存 Hash Join
Fallback: 若递归分区无法显著缩小分区，改用两侧外排 + Merge Join
```

实现会为左右分区分别创建 Disk-backed Sorter，再构造 Merge Join Operator。源码见 [external_hash_joiner.go](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L24-L55) 与 [Sort-Merge fallback 构造](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L83-L138)。

这个设计给出了一个很重要的边界：**运行时切换不必重做整个查询计划，它可以只针对已经隔离出来的坏分区。** 已完成分区仍然走 Hash，缺乏进展的分区才付 Sort 成本。

### 3.2 TiDB：Merge/Stream 依赖属性，Hash 负责通用路径与 spill

TiDB 的 `GetMergeJoin` 先读取逻辑 Join 的左右可提供属性，再检查：

- 两侧 join key 是否能完整命中排序前缀；
- `ENUM/SET` 与 collation 是否兼容；
- 当前是否含尚未支持的 null-safe join key；
- 上层 Required Property 能否传给两侧。

关键实现见 [PhysicalMergeJoin candidate generation](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L49-L124) 和 [child required property derivation](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L425-L453)。这说明 Merge Join 的核心优势不是“比较成本常数更小”，而是**能够消费并继续传递顺序**。

Stream Aggregate 沿用了同一套属性逻辑：只有 Group By 是可排序列、child 能提供匹配顺序、task type 支持时才生成普通候选；另一路 enforced candidate 会显式给 child 增加 Group Key Ordering。源码见 [PhysicalStreamAgg candidate generation](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_stream_agg.go#L39-L155)。这也澄清了一个术语误区：Stream Aggregate 不是 Hash Aggregate 的“低内存开关”，而是建立在 Ordering 之上的另一条物理路径。

TiDB 的 Cost Model V2 则表现出另一个现实：模型仍然包含经验参数，但状态表达已经比标量行数丰富。Hash Join 区分 build/probe，使用 build row size、join key 数、CPU/Memory factor 与并发；Merge Join 分别计算 filter/group 成本，并有独立 cost factor。源码见 [Hash/Merge cost V2](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/plan_cost_ver2.go#L737-L820)。

执行侧的 Hash Join V2 把内存错误变成了一个分区化恢复过程：

```text
build rows -> partitions -> row tables -> hash table
                     |
                     +-- memory pressure -> spill selected partitions
                                              |
probe rows -----------------------------------+-> matching probe partitions spill
                                              |
                                  restore one partition -> rebuild -> reprobe
```

它把 `spillAction` 注册到 `MemTracker`（`FallbackOldAndSetNewAction`），由内存压力触发分区 spill；`collectSpillStats` 记录每轮 bytes、spilled partition 数和 round；恢复阶段从 `spillHelper.stack` 中逐个 `pop` 分区，重建 hash table 后 reprobe。源码见 [HashJoinV2 spill initialization](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L654-L767) 和 [partition restore loop](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L1103-L1164)。

递归分区还带一个明确的终止条件。`initMaxSpillRound` 按 `log(1024) / log(partitionNumber)` 计算最大轮数，即允许分区总数递归扩张到 1024 为止；若初始分区数已超过 1024，则 `maxSpillRound = 1`，直接放弃继续细分。§6.3 会把“自适应是否有终止条件”列为一个通用风险，这里可以看到一个具体答案：用**分区总数上界**而非轮数上界兜底，因为决定 I/O 放大的是累计分区数，不是递归深度。

这里也暴露出模型与执行器之间常见的断层：执行器已经知道 spill round、每轮字节数与分区数，优化器却未必把这些分布作为一等反馈信号。真正有价值的闭环，不是只收集“该 Join 用时多少”，而是把这些结构化运行指标反馈到下一次 crossover 判断。

### 3.3 OceanBase：显式拆开 Hash、Merge、Sort 与 Group 成本

OceanBase 的 Cost Model 是理解“公式应如何对应实现”的好样本。

Hash Join 成本显式包含：

```text
build materialization
+ build hash key computation
+ build hash-table insertion
+ probe hash key computation
+ probe lookup
+ join filter build/use
+ residual predicate
+ output cost
```

Merge Join 则包含左右取数、条件比较、输出和一侧 materialization。源码见 [cost_mergejoin](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L285-L351) 与 [cost_hashjoin](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L353-L430)。

Sort 也不是一个公式包打天下。`cost_sort` 的入口是一串按优先级排列的 `else if`：

- `is_local_merge_sort_` -> Local Merge Sort；
- `prefix_pos_ > 0` -> Prefix Sort；
- `part_cnt_ > 0 && topn_ >= 0` -> Partition Top-N；
- `topn_ >= 0` -> Top-N；
- `part_cnt_ > 0` -> Partition Sort；
- 其余 -> Normal Sort。

这个顺序本身承载语义：Partition Top-N 必须先于 Top-N 判断，否则带 `part_cnt_` 的计划会被当成全局 Top-N 计价；Prefix Sort 又必须先于两者，因为已排序前缀会直接改变需要排序的行数。源码见 [cost_sort dispatch](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L510-L576)。这种细分非常重要：若把 Top-N 也按全量 `N log N` 估算，或忽略已排序前缀，Sort 会被系统性高估——而按 §4.2 的逻辑，系统性高估 Sort 就等于系统性否决所有依赖 Ordering 的 Merge/Stream 路径。

Group By 侧则分别存在 `cost_merge_group` 与 `cost_hash_group`。Hash Group 会计入结果物化、build/probe hash 与 key hash；Merge Group 更接近顺序扫描与聚合函数求值。源码见 [group cost](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L1125-L1187)。

它带来的启发不是照搬系数，而是：**一个可校准模型必须先有足够细的成本项；把所有差异压成 `rows * factor` 后，再多训练数据也很难解释错误来自哪里。**

### 3.4 Trino：不比较 Merge Join，把问题转成 Join Distribution

Trino 当前通用 Join 路径以 Hash/Lookup Join 为核心。它的关键选择不是 Hash 与 Merge，而是：

```text
build side = left or right
distribution = REPLICATED or PARTITIONED
spill = enabled or disabled
dynamic filter = produce/consume
```

`DetermineJoinDistributionType` 会枚举左右翻转以及 Broadcast/Partitioned 候选，用 input/exchange/build/probe cost 比较。但在比较之前，它先做了 §2.2 所说的可行性过滤：`mustPartition` 在 Join Type 为 `RIGHT` 或 `FULL` 时直接排除 REPLICATED，因为此时 build side 是右侧，复制它会把未匹配的右表行重复多份；`mustReplicate` 则反向排除 PARTITIONED。只有两侧都合法的候选才进入成本比较。

成本未知时，规则退回 `getSizeBasedJoin`：先用源表大小与 `join-max-broadcast-size` 判断能否 REPLICATED，两侧都超限时再用 `getFirstKnownOutputSizeInBytes` 比较，并以 `SIZE_DIFFERENCE_THRESHOLD = 8` 决定哪一侧做 build。这个 8 倍不是调优参数，而是对估计不可信的防御：源码注释指出，该函数可能因为缺乏估计而没有扣除 filter 或 aggregation 带来的缩减，因此**只有当差异大到不可能被这层误差解释时，才敢翻转 build side**。这与 §5.2 的主题完全一致：信号强度应该随估计可信度调整，而不是固定阈值。源码见 [DetermineJoinDistributionType](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/DetermineJoinDistributionType.java#L80-L169)。

执行计划构造阶段决定是否启用 Spilling Join 时，除了 session 开关与 `node.isSpillable()`，还有一个容易忽略的条件：`!buildOuter`。当 build 侧需要输出未匹配行（RIGHT/FULL Join）时直接禁用 spill，因为匹配状态无法跨分区安全恢复；同时开启 spill 会强制要求 fixed distribution（`getDriverInstanceCount` 必须存在），否则分区号在 driver 间对不上。启用后 build side 创建 `PartitionedLookupSourceFactory`，probe side 则按 spill partition 分批消费和恢复。源码见 [LocalExecutionPlanner join path](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/LocalExecutionPlanner.java#L2869-L2983) 与 [SpillingJoinProcessor](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/operator/join/spilling/SpillingJoinProcessor.java#L47-L155)。

这条路线常被误解为“算法不完整”。更准确的说法是：Trino 选择了较小的实现集合，用更少的策略分叉换取一致的分布式执行、Dynamic Filter 和 spill 语义；省下的复杂度并没有消失，而是转移到了 Join Type 与 distribution、spill 的语义合法性矩阵上——而这些约束即使引入 Merge Join 也一条都少不了。真正的代价是无法直接利用双方已有顺序做 Merge Join，也无法用一种内存更稳定的 Join 算法对冲 Hash 风险。

### 3.5 Apache Doris：向量化 Hash Join + 分区 spill，排序服务于独立语义

Doris 的 Nereids Cost Model 对 Join 的核心输入是 probe/build/output rows，并加入统计可信度、Join Cluster width 与 Runtime Filter 连通性的启发式调整；Quick Sort/Top-N 则单独计价，并对单阶段 Gather Sort 增加显著惩罚。源码见 [PhysicalQuickSort cost](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/CostModel.java#L257-L284) 与 [PhysicalHashJoin cost](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/CostModel.java#L384-L444)。

`visitPhysicalHashJoin` 里有几处值得单独拿出来看，因为它们把 §5.3 “Cost Model 的任务是选对相对顺序”落到了字面上：

- 当两侧行数相等且统计不可信时，代码比较两侧的 `computeConnectivity`，并对较弱一侧做 `leftRowCount += 1`；
- 当两侧行数与 Join Cluster width 都相等时，再比 tuple size，对 build 侧数据更多的一方做 `leftRowCount += 1e-3`；
- 若 build 侧宽度为 1 且能产生有效 Runtime Filter，则从 `leftRowCount` 中减去一个 `bonus`，以偏好 `A-B-filter(C)` 而非 `A-filter(C)-B` 的 Join Order。

也就是说，它并不试图把这些启发式折算成真实的毫秒或字节，而是**直接对行数做微量扰动，只为了改变候选的排序结果**。`+= 1` 与 `+= 1e-3` 的量级差别还表达了优先级：connectivity 强于 tuple size。这种做法在建模上并不优雅，却恰好说明一件事：既然优化器只消费相对顺序，那么在排序信息上直接编码意图，比构造一个虚假的绝对成本更诚实。

执行器在允许 spill 且非 Broadcast Join 时构造 `PartitionedHashJoinProbeOperatorX` 与 `PartitionedHashJoinSinkOperatorX`，否则使用普通 Hash Join pipeline。注意这里 Broadcast 被排除在 spill 之外：各 instance 持有完整的 build side 副本，分区 spill 并不能降低单个 instance 的峰值内存，而 Broadcast 另有 `enable_share_hash_table_for_broadcast_join` 一路共享 hash table 来缓解同一问题。源码见 [Doris partitioned hash join construction](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/pipeline/pipeline_fragment_context.cpp#L1761-L1810)。

这再次说明，现代向量化 MPP 引擎可能不把 Sort-Merge Join 放进常规竞争集合，而是把主要复杂度投入：

- Join Order 与 build side；
- Colocate/Bucket Shuffle/Broadcast/Shuffle；
- Runtime Filter；
- 向量化 Hash Table 特化；
- Partitioned Spill。

### 3.6 五个系统的决策边界

| 系统 | 优化期主决策 | Physical Property | 运行时兜底 | 主要盲区/代价 |
|---|---|---|---|---|
| CockroachDB | Hash/Merge/Lookup 竞争 | Ordering 进入 Memo | Grace Hash；坏分区转 Sort-Merge | 成本仍有经验系数 |
| TiDB | Hash/Merge/Index；Hash/Stream Agg | Ordering 决定候选与传递 | Hash Join V2 分区 spill/restore | 优化期 spill 风险表达仍有限 |
| OceanBase | 丰富的 Join/Sort/Group 成本 | Ordering、Distribution 共同影响 | Work Area/外部执行 | 模型复杂，校准成本高 |
| Trino | Build side + Broadcast/Partitioned | 重点是 Distribution | Partitioned spilling | 不直接利用 Merge Join 算法空间 |
| Doris | Join Order + Distribution + Hash Join | Distribution/Runtime Filter 更突出 | Partitioned Hash Join spill | Sort 与 Join 的属性复用空间较窄 |

没有哪个系统提供了“标准答案”。它们更像五种工程预算分配：搜索空间越丰富，优化器状态与成本校准越复杂；算法集合越窄，执行器的鲁棒性要求越高。

---

## 4. 真正决定 crossover 的变量

### 4.1 Cardinality 不够，还要有 bytes、NDV 与 multiplicity

Hash Table 的内存并不与输出行数简单相等：

```text
hash_table_bytes
  ~= directory_bytes(load_factor)
   + distinct_key_bytes
   + payload_rows * payload_width
   + duplicate_chain_or_adjacency_bytes
   + allocator_and_alignment_overhead
```

至少要区分：

- `build_rows` 与 `probe_rows`；
- `build_bytes` 与 `probe_bytes`；
- join/group key NDV；
- 最大频次、Top-K 频次与重复度；
- key width、payload width 和变长字段比例；
- Join Selectivity 与输出 multiplicity。

例如，`NDV` 很低并不一定让 Hash Join 更省：若保留全部 build payload，大量相同 key 仍需存储，并可能形成很长的匹配列表；若结果是 `n:m`，真正危险的是 output expansion，而非 Hash Table directory。

### 4.2 Existing Ordering 的价值必须跨算子计算

排序成本应拆成：

```text
full_sort
prefix_sort
incremental_sort
top_n
merge_runs
order_preservation
```

并区分以下情况：

| 输入状态 | Join/Agg 需要 | 真实附加成本 |
|---|---|---|
| 两侧按完整 Join Key 有序 | Merge Join | 接近线性 merge |
| 仅一侧有序 | Merge Join | 另一侧 Sort + merge |
| 两侧仅有相同前缀 | Merge Join | 对各 prefix group 补排 |
| Group Key 已聚集 | Stream Aggregate | 单组状态，近似线性 |
| 无序但下游最终要求同一顺序 | Sort + Merge/Stream | Sort 成本可被下游复用 |
| 无序且无下游顺序需求 | Hash | 通常更有利 |

核心原则是：**Sort 是生产属性的 Enforcer，Merge/Stream 是消费属性的 Operator。** 不把二者分开，模型就无法表达“为后续算子提前投资”。

### 4.3 Memory Budget 产生的不是线性惩罚，而是相变

Hash 的危险区域通常在：

```text
estimated_hash_bytes / effective_memory_budget ~= 1
```

一旦越界，执行路径可能从一次 build/probe 变成：

```text
partition -> write -> read -> rebuild -> reprobe
```

若某些分区仍过大，还会出现递归轮次。Sort 也会 spill，但外排更接近可预测的 run generation + k-way merge。两者都不是“spill 一次加固定 penalty”。

建议保留 operator memory curve：

```text
Cost(op, M) -> expected runtime under memory M
```

而不是只存一个 `peak_memory`。在多查询并发下，`M` 是运行时变量，优化器应看到一个区间或分布，而非编译时常数。

### 4.4 Skew 的本质是 max，而平均数会掩盖它

分布式 Hash 的完成时间更接近最慢分区：

```text
T_stage ~= max(T_partition_1 ... T_partition_p) + coordination
```

所以 `avg_rows_per_partition` 对尾延迟不够。至少需要：

```text
max_partition_ratio
top_k_frequency
HHI or entropy
heavy_hitter_bytes
estimated_output_multiplicity
```

倾斜同时放大四种风险：单分区内存、spill rounds、网络不均衡和 probe 长链。Sort/Range Partition 也会受倾斜影响，但更容易通过采样 boundary、局部切分与多路 merge 显式观察范围大小。

### 4.5 网络经常比本地算法更先决定胜负

在 MPP 中，Join Cost 至少是：

```text
network_cost
  = partition_or_range_cpu
  + serialization
  + compression
  + bytes_on_wire
  + receiver_deserialization
  + backpressure
  + straggler_penalty
```

Broadcast 的成本不是 `build_bytes`，而接近 `build_bytes * receiver_count`；Hash Shuffle 与 Range Shuffle 都要发送两侧数据，但 range boundary 的采样与全局顺序可能为下游带来收益。

这也是为什么在 Trino、Doris 一类系统中，“Broadcast 还是 Partitioned”往往比“Hash 还是 Merge”更重要。

### 4.6 Hardware Profile 会移动边界

Hash 热路径依赖：

- hash instruction throughput；
- random access latency；
- cache/TLB miss；
- prefetch 效果；
- load factor 与冲突处理；
- NUMA placement；
- probe branch predictability。

Sort/Merge 热路径依赖：

- comparator/key normalization；
- SIMD sorting network/radix sort；
- sequential bandwidth；
- merge fan-in；
- materialization bytes；
- storage read/write bandwidth。

同一套常数不能跨 CPU 代际、DRAM/CXL/NVMe、压缩格式和向量宽度长期有效。Cost Model 应绑定 hardware profile，并通过微基准周期性重校准。

---

## 5. 成本表示的缺口：从资源分项到风险

### 5.1 为什么一个数字不够

优化器最后需要排序 Candidate，但不意味着分析时只能观察一个标量。把五个系统的成本表示拉出来看，它们恰好构成了一个谱系：分歧不在“要不要分项”，而在“分项在哪一步被折叠，以及折叠前能不能被利用”。

Trino 的 `LocalCostEstimate` 是三维结构：

```java
private final double cpuCost;
private final double maxMemory;
private final double networkCost;
```

它向上汇总为 `PlanCostEstimate` 时还会带上 `inputDataSize`。真正的比较发生在 `CostComparator.compare`：它先用 `checkArgument(!left.hasUnknownComponents() && !right.hasUnknownComponents(), "cannot compare unknown costs")` 拒绝比较任一维度未知的候选，然后才做加权求和：

```java
double leftCost = left.getCpuCost() * cpuWeight
        + left.getMaxMemory() * memoryWeight
        + left.getNetworkCost() * networkWeight;
```

其中 `checkArgument` 那一步是 §2.2 “先做可行性过滤”的另一种形式——**维度缺失时不要用一个编出来的标量继续比较**。源码见 [LocalCostEstimate](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/LocalCostEstimate.java#L33-L35) 与 [CostComparator.compare](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/CostComparator.java#L54-L71)。

`compare` 里还有一条比实现更能说明问题的 TODO：

```java
// TODO when one left.getMaxMemory() and right.getMaxMemory() exceeds query memory limit
//      * configurable safety margin, choose the plan with lower memory usage
```

翻译过来就是：当某个候选的 `maxMemory` 超过查询内存上限乘以一个安全边距时，应当直接选内存更低的那个，而不是继续比加权和。这正是 §5.2 要用 `RiskAwareScore` 表达的东西，也是 §4.3 “内存不足是相变而不是线性惩罚”的直接推论：加权和允许一个内存超限但 CPU 很便宜的计划胜出，而它实际上会 spill 或失败。Trino 把这件事写成 TODO 而不是已实现，恰好说明**维度保留下来了，并不等于风险建模已经跟上**。

Doris 的 `Cost` 同样是三维（`cpuCost`/`memoryCost`/`networkCost`），但它在构造函数里就立即折成了标量：

```java
CostWeight costWeight = CostWeight.get(sessionVariable);
this.cost = costWeight.cpuWeight * cpuCost + costWeight.memoryWeight * memoryCost
        + costWeight.networkWeight * networkCost;
```

三个权重分别来自 `cbo_cpu_weight`、`cbo_mem_weight` 与 `cbo_net_weight` 会话变量。也就是说，本文后面当作示意公式写出的 `score = w_cpu * cpu + w_mem * memory + ...`，在 Doris 里就是一次生产代码的构造调用。分项虽然作为字段保留下来并有 getter，但参与排序的是那个 `cost`。源码见 [Doris Cost](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/Cost.java#L31-L52)。

把 Trino 与 Doris 并置，才能看出真正的差别：两者都是三维，分歧在于**折叠发生在哪一步**。Doris 在构造函数里就把分项折成 `cost`，调用方拿到对象时分项已经混在一起；Trino 则把分项一路传递到 comparator 才折叠，因此它能按 session 调整权重，也能在比较前看到完整分项。这也解释了为什么 Trino 能在 `compare` 里留下那条关于内存上限的 TODO——它至少还看得到 `maxMemory`。

TiDB 的 `CostVer2` 则把分项彻底移出了成本值本身：

```go
type CostVer2 struct {
    cost  float64
    trace *CostTrace
}

// CostTrace record the basic factor and formula in cost est.
type CostTrace struct {
    factorCosts map[string]float64 // map[factorName]cost, used to calibrate the cost model
    formula     string             // It used to trace the cost calculation.
}
```

参与比较的仍是一个 `float64`；分项存在 `trace.factorCosts` 里，按 factor 名（`cpuFactor`、`memFactor`、`netFactor`、`scanFactor`、`requestFactor`）累加，另有一条供 `EXPLAIN ANALYZE` 输出的可读 `formula`（如 `hashmem(rows*size*factor)`）。源码注释把用途写得很直接：`used to calibrate the cost model`。这是一个值得注意的分工：**分项不改善本次选择，它改善的是下一次校准**，而这恰好就是 §3.2 所说“把结构化运行指标反馈回 crossover 判断”的优化器侧入口。源码见 [CostVer2 与 CostTrace](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/util/costusage/cost_misc.go#L49-L68)。

剩下两个系统用标量，但各自用不同方式补回标量丢掉的约束。OceanBase 的所有 `cost_*` 函数都把结果写回一个 `double &cost` 输出参数，细分体现在**函数入口的分支**（§3.3 的六种 Sort）而不是输出的维度上。CockroachDB 的 `memo.Cost` 主体也是标量 `C float64`，但比较函数 `Less` 是字典序的：

```go
func (c Cost) Less(other Cost) bool {
    if c.Penalties != other.Penalties {
        return c.Penalties < other.Penalties   // 先比位掩码
    }
    const ulpTolerance = 1000
    return math.Float64bits(c.C)+ulpTolerance <= math.Float64bits(other.C)
}
```

`Penalties` 是一个 `uint8` 位掩码，`HugeCostPenalty`（被 hint 强制）、`FullScanPenalty`、`UnboundedCardinalityPenalty` 等按位从高到低排列。只要两个候选的掩码不同，标量 `C` 根本不参与比较。这又是 §2.2 的一种实现：与其给不可行候选算一个极大 cost，不如把它们放到一个独立的、优先级更高的比较层级。它还用 ULP（units of least precision）而非固定 epsilon 处理浮点相等，因为允许的误差应当与数量级成正比。源码见 [memo.Cost 与 Less](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/memo/cost.go#L17-L58)。

把这五种形态与 Hash/Sort 决策所需的信息放在一起，才能看出真正的缺口在哪：

```text
CostVector {
    startup_ns              <- 五个系统均无
    total_cpu_ns            <- cpuCost / cpuFactor
    io_read_bytes           <- scanFactor（仅 TiDB 区分）
    io_write_bytes          <- 五个系统均无
    network_bytes           <- networkCost / netFactor
    peak_memory_bytes       <- maxMemory / memoryCost / memFactor
    expected_spill_bytes    <- 五个系统均无
    expected_spill_rounds   <- 五个系统均无
    output_property_value   <- 五个系统均无（Ordering 收益不在成本里）
    confidence              <- 五个系统均无
}
```

这张对照表比抽象出一个理想模型更有用：Trino、Doris、TiDB 已经证明分项表示是可行的工程实践，但它们的维度集中在 cpu/mem/net 这类**已经发生的资源消耗**；而 Hash/Sort crossover 真正需要的 `spill_bytes`、`spill_rounds`、`output_property_value` 和 `confidence` 恰好都不在里面。§3.2 提到的断层在这里得到印证：执行器已经统计出 spill round 与每轮字节数，成本表示却没有对应的维度去承接它们。

如果把不同维度折算成标量，其关系可以示意为：

```text
score
  = w_startup * startup
  + w_cpu     * cpu
  + w_io      * io
  + w_net     * network
  + w_mem     * memory_pressure
  + w_tail    * tail_risk
  - w_prop    * downstream_property_benefit
```

这两种表达方式揭示了一个区别：标量负责给候选排序，分项成本负责解释排序。在分析错误计划时，只有保留分项视角，才能区分 `build_bytes` 误估、spill 曲线失真、Ordering 收益漏算和网络成本偏差。对照上面那张表可以看到，真正的差距不在“要不要分项”，而在“分项里少了哪几维”：把 spill 与属性收益纳入同一套表示，才是让 crossover 可解释的前提。

### 5.2 用期望惩罚表达不确定性

统计估计不是一个点，而是带置信度的分布。设 `s` 为运行时真实状态，候选计划为 `p`：

```text
ExpectedCost(p) = E_s[Cost(p, s)]

Penalty(p, s) = Cost(p, s) - min_q Cost(q, s)

RiskAwareScore(p)
  = E_s[Cost(p, s)]
  + lambda * TailRisk(Penalty(p, s))
```

这里的 `TailRisk` 是研究问题的抽象表达，并不意味着前述五个系统已经实现了相同的风险评分。P95/P99 penalty、CVaR 或超过 SLA 的概率分别代表不同的目标；它们共同提醒我们，一个平均值略好但一旦 spill 就慢十倍的 Hash Plan，未必适合高并发或延迟敏感场景。

2024 年的 PARQO 把鲁棒计划选择定义为基于选择率误差分布的 expected penalty，并使用敏感性分析降低参数维数。这与 Hash/Sort crossover 非常契合：无需为所有统计量构造高维分布，只需优先分析最敏感的几个维度，例如 build bytes、heavy hitter ratio 与可用内存。论文见 [PARQO: Penalty-Aware Robust Plan Selection in Query Optimization](https://www.vldb.org/pvldb/vol17/p4627-xiu.pdf)。

### 5.3 优化目标应该看 ranking 和 regret

Cost Model 的任务是选对相对顺序，而不是预测一个绝对毫秒数。因此评估至少包括：

```text
pairwise_accuracy
  = P(score(chosen) < score(alternative)
      agrees with runtime ordering)

regret
  = runtime(chosen) / runtime(best_candidate)
```

`MAPE(cost, runtime)` 可以很低，仍然可能在 crossover 附近频繁选错；反过来，绝对成本偏差很大，只要候选排序正确，也能产生好计划。2023 年 Lero 的核心观点正是：直接学习计划的 pairwise ranking，往往比回归绝对 latency 更贴近优化目标。论文见 [Lero: A Learning-to-Rank Query Optimizer](https://www.vldb.org/pvldb/vol16/p1466-zhu.pdf)。

从这些研究可以得到一个更审慎的判断：学习模型能够改善候选排序，却不能消除语义、物理属性和内存可行性的约束。它究竟应承担成本校正、候选重排还是更大范围的搜索责任，仍取决于训练数据、候选覆盖率和失效边界，不能仅凭预测精度下结论。

---

## 6. 运行时自适应：不要把“切换算法”理解为重启算子

### 6.1 三种安全的 adaptive barrier

并不是任何时刻都适合从 Hash 切到 Sort。Hash Table 建了一半后直接丢弃并重排两侧，通常会浪费大量工作。更可行的边界是：

1. **Operator 内部分区边界**：只对尚未处理或无法收敛的 spill partition 切换；
2. **Pipeline/Stage 边界**：Shuffle 已物化后，用真实 rows/bytes/NDV 重新选择下一阶段；
3. **重复查询边界**：保留结构化运行画像，下一次编译调整策略。

```text
Plan Time
  candidate generation
    -> initial plan + alternatives metadata

Run Time
  build/sample/shuffle
    -> observe rows, bytes, skew, memory
      -> continue
      -> repartition/spill
      -> per-partition Hash -> Sort-Merge
      -> next-stage reoptimize

Feedback Time
  profile by plan signature + data epoch + hardware class
```

### 6.2 运行时需要观察什么

从解释运行时行为的角度，以下观测维度尤其重要。它们是调研中需要区分的指标，不代表每个系统已经完整暴露了这些 telemetry：

| 维度 | 指标 | 用途 |
|---|---|---|
| 输入 | actual rows/bytes/row width | 修正 cardinality 与 materialization |
| Key | NDV、Top-K、NULL ratio、max frequency | 预测 Hash collision、长链与 skew |
| Hash | table bytes、load factor、probe count、chain length | 校准 build/probe 成本 |
| Spill | trigger bytes、partition count、rounds、read/write bytes | 学习内存曲线与失败模式 |
| Sort | run count、merge passes、compare/key-normalize time | 校准内排/外排边界 |
| Network | send/receive bytes、compression ratio、straggler | 校准 distribution 代价 |
| Property | input/output ordering、reused-by operators | 衡量 Ordering 实际收益 |

这些指标的可比性依赖逻辑表达式、数据版本/分区和 hardware profile。只按 SQL 文本比较运行画像，容易把参数变化、数据演化和硬件差异混在一起，从而把资源变化误判为算法优劣。

### 6.3 从外部 Hash Join 观察降级路径

前述 CockroachDB 外部 Hash Join 的实现展示了一种分区级退路：先尝试分区和恢复 Hash；当递归分区无法继续缩小工作集时，改用外排和 Merge Join。下面只概括这条执行路径，不是跨项目通用状态机，也不表示 TiDB、Trino 或 Doris 具备相同的 Hash-to-Merge fallback。

```text
                 +----------------------+
                 | IN_MEMORY_HASH_BUILD |
                 +----------+-----------+
                            |
                 memory pressure / skew
                            v
                 +----------------------+
                 | PARTITION_AND_SPILL  |
                 +----------+-----------+
                            |
             +--------------+---------------+
             |                              |
       partition fits              no progress / too many rounds
             v                              v
    +------------------+          +---------------------+
    | REBUILD_AND_PROBE|          | SORT_PARTITION_PAIR |
    +------------------+          +----------+----------+
                                             |
                                             v
                                  +---------------------+
                                  | MERGE_JOIN_PARTITION|
                                  +---------------------+
```

阅读这类路径时，关键不只是能否切换，而是如何维持执行不变量：

- 只在语义等价的候选间切换；
- 已输出结果不能重复或遗漏，Outer/Semi/Anti Join 尤其需要匹配状态；
- 切换依据必须包含“继续 Hash 是否有进展”，而不是只看内存超限；
- spill round 和重试是否有终止条件，否则自适应本身可能成为抖动源；
- 切换原因是否可观测，否则难以区分统计错误、资源压力和分区不收敛。

### 6.4 资源自适应比算法自适应更基础

如果多个算子都把内存视为固定配额，单算子即使能 spill，也可能发生集体抖动。2025 年 CIDR 的 Paged Memory Management 工作把 Sort、Aggregate、Hash Table 等 stateful operator 的 scratch space 放到统一 page 管理下，探索按成本动态转移内存和 query context switch。它的意义在于：Hash/Sort crossover 本身依赖 `M`，而 `M` 应当是一个可被调度的运行时资源。见 [Resource-Adaptive Query Execution with Paged Memory Management](https://www.vldb.org/cidrdb/papers/2025/p2-otaki.pdf)。

---

## 7. 学术界的演进：问题从算法胜负走向鲁棒执行

### 7.1 经典阶段：外存模型与参数化选择

Graefe 在 1993 年的综述系统化整理了 Sort、Hash、Hybrid Hash、Merge 与磁盘 I/O 的关系。经典外存模型建立了今天仍然有效的框架：内存大小决定 run/partition 数，溢写轮数决定 I/O；但当时的主要瓶颈仍以磁盘页和顺序/随机 I/O 为中心。见 [Query Evaluation Techniques for Large Databases](https://doi.org/10.1145/152610.152611)。

1992 年 Parametric Query Optimization 已经认识到 buffer size 等参数在优化时可能未知：与其只产出一个点最优计划，不如求一组在不同参数区域最优的计划。见 [Parametric Query Optimization](https://www.vldb.org/dblp/db/conf/vldb/IoannidisNSS92.html)。这可以直接映射到今天的 Hash/Sort 问题：为 memory、build bytes、skew 构造少量决策区域，而不是用单一阈值。

1998 年的 Mid-Query Re-Optimization 则讨论如何在执行中检测次优计划并纠正。见 [Efficient Mid-Query Re-Optimization of Sub-Optimal Query Execution Plans](https://www.vldb.org/dblp/db/conf/sigmod/KabraD98.html)。现代 stage-level AQE 延续了同一思想，只是 barrier 从 materialization point 扩展到了 Shuffle、pipeline 和 spill partition。

### 7.2 多核阶段：硬件改变常数，但没有宣布永久胜者

2009 年 Kim 等人同时高度优化 Hash Join 与 Sort-Merge Join。他们当时测得 Hash 更快，但分析认为更宽 SIMD 与更低的单核内存带宽可能让 Sort-Merge 获得优势。见 [Sort vs. Hash Revisited: Fast Join Implementation on Modern Multi-Core CPUs](https://www.vldb.org/pvldb/vol2/vldb09-257.pdf)。

2013 年 Balkesen 等人在共同平台重新实现并对比多种算法，结论是 Radix Hash 在大多数测试中仍更快，Sort-Merge 只在非常大的输入上接近；输入规模、并行度、NUMA、skew 和实现细节都会改变结论。见 [Multi-Core, Main-Memory Joins: Sort vs. Hash Revisited](https://www.vldb.org/pvldb/vol7/p85-balkesen.pdf)。

两篇论文并不矛盾。它们共同证明：**crossover 会随硬件和实现移动，论文中的胜负不能直接固化成优化器规则。**

### 7.3 真实系统阶段：最漂亮的微基准未必是最好默认值

2021 年 Bandle、Giceva 与 Neumann 把 Radix Join 集成到 Umbra，并与优化的 Non-partitioned Hash Join 比较。虽然 Radix Join 在窄 payload、单 Join 微基准上很强，TPC-H 中对最终不会命中的 tuple 做 partition/materialization 的成本却经常抵消收益；加入 Bloom Filter 后改善明显，但 Non-partitioned Hash Join 仍更稳定。见 [To Partition, or Not to Partition, That is the Join Question in a Real System](https://db.in.tum.de/~bandle/papers/bandle-partitionVsNonPartition.pdf)。

这项工作对策略选择有三个直接启发：

1. benchmark 必须覆盖完整 pipeline，而非只测 kernel throughput；
2. join selectivity 与 payload width 是一等变量；
3. 默认算法应优化“稳定表现”，专用算法只在可判定区域启用。

### 7.4 鲁棒性阶段：优化 Hash 内部结构，也改变选择边界

2024 年的 Unchained Hash Table 将 build-side partitioning、adjacency array、pipelined probes、Bloom Filter 与 software write-combine buffer 组合起来，目标不是某个单点最快，而是同时应对选择性 probe、重复键、并行 build 和 skew。论文报告其在关系查询上平均优于 open addressing，并在含大量重复的图查询上显著改善。见 [Simple, Efficient, and Robust Hash Tables for Join Processing](https://db.in.tum.de/~birler/papers/hashtable.pdf)。

同年 CIDR 的 Perfect Hashing 研究在真实 OLAP 系统中评估 PHF，报告 Join 与 Aggregate 的端到端收益，同时指出 build time 与 probe throughput 仍需共同优化。见 [Is Perfect Hashing Practical for OLAP Systems?](https://www.vldb.org/cidrdb/2024/is-perfect-hashing-practical-for-olap-systems.html)。

这两项进展说明：优化器的 `HASH` 不应永远指向一个实现。内部还可选择：

```text
generic hash table
open addressing
chaining / adjacency layout
radix partitioned hash
perfect/direct hash
skew-specialized partition
```

如果执行器具备多种 Hash Table，外层 Hash-vs-Sort 模型就必须先知道“Hash 的哪个实现”参与比较。

### 7.5 2025-2026：决策继续下沉到运行时和内存层级

2025 年 Adaptive Factorization 工作把因子化聚合与 Worst-case Optimal Join 集成到 DuckDB，并把是否启用的决定推迟到运行时：Hash build 阶段顺便构造轻量 sketch，再由启发式或模型判断是否值得避免中间结果展开。见 [Adaptive Factorization Using Linear-Chained Hash Tables](https://www.vldb.org/cidrdb/papers/2025/p21-gro.pdf)。它提示我们：**build phase 不只是不可撤销的成本，也可以是低成本采样点。**

2026 年 CIDR 的 CXL Hash Join 研究进一步把问题从“在哪执行”推进到“哪些数据值得移动”：将全部 CXL 数据搬到 DRAM-interleaved tier 并非总是最优，部分搬移可能以更少数据移动获得更均衡的带宽。见 [Hash Joins Meet CXL: A Fresh Look](https://www.vldb.org/cidrdb/2026/hash-joins-meet-cxl-a-fresh-look.html)。

截至 2026 年，研究路线已经从：

```text
Hash vs Sort
    -> partitioned vs non-partitioned Hash
    -> hash-table layout / filter / factorization
    -> robust plan under estimation error
    -> adaptive memory and heterogeneous memory placement
```

这不是说 Sort 不再重要，而是说明“算法选择”的粒度不断细化，运行时状态与数据移动正成为第一公民。

---

## 8. 如何阅读实验：从 crossover point 到 crossover surface

### 8.1 单条曲线遗漏了哪些变量

判断论文或 benchmark 结论能否外推时，需要检查下列维度是否被覆盖。表中的范围用于说明可能的负载跨度，不是一份产品实施或验收清单：

| 维度 | 典型变化范围 |
|---|---|
| Build rows | `10^3` 到 `10^9`，对数采样 |
| Probe/build ratio | `1`, `4`, `16`, `64`, `256` |
| Row width | `8B`, `32B`, `128B`, `512B`, 变长字符串 |
| NDV/build rows | `1.0`, `0.5`, `0.1`, `0.01` |
| Skew | uniform；Zipf 多档；单 heavy hitter |
| Selectivity | `0%`, `1%`, `10%`, `100%` |
| Memory/input bytes | `0.1`, `0.25`, `0.5`, `1`, `2` |
| Ordering | none；单侧；双侧；prefix；reverse |
| Distribution | colocated；broadcast；hash/range shuffle |
| Concurrency | 单查询到内存/带宽饱和 |
| Hardware | CPU/SIMD/NUMA、DRAM、NVMe、CXL class |

“数据量 - 时间”二维曲线只能展示其余条件固定时的一个切片。更完整的研究对象是条件区域：

```text
best_strategy
  = f(build_bytes,
      probe_bytes,
      memory_ratio,
      skew,
      selectivity,
      existing_order,
      network,
      concurrency,
      hardware)
```

### 8.2 三层 Benchmark 回答不同问题

**Kernel 层**验证实现常数：

- hash key、insert、probe throughput；
- sort key normalization、run generation、merge throughput；
- 不同 Hash Table layout 的 collision/duplicate 行为。

**Operator 层**验证资源曲线：

- 首行/总耗时；
- peak memory；
- spill bytes/rounds；
- skew 下最慢 partition；
- Outer/Semi/Anti Join 的额外状态。

**Pipeline/Query 层**验证全局属性收益：

- Join 后继续 Aggregate/Window/ORDER BY；
- Runtime Filter 是否让 partition work 失去价值；
- 多 Join 是否产生大中间结果；
- 并发下内存与 I/O 是否互相干扰。

微基准决定公式中的基础常数，完整查询决定候选是否值得存在。两者不能互相替代。

### 8.3 Counterfactual：未被选中的计划如何评价

只记录被选计划，无法知道备选计划会有多快。这也是 cost model 和学习优化器研究中的反事实难题：在相同 snapshot 与 resource class 下比较强制候选，才能把算法差异和环境变化尽量分开。

这类比较可以概括为：

```text
same query + same snapshot + same resource class
  force hash
  force merge/sort
  force broadcast/partitioned
  vary memory budget
```

分析结果时值得区分的维度包括：

```text
chosen_strategy
best_observed_strategy
regret
pairwise_label
reason_code
estimated/actual cost vector
```

这类证据可以帮助研究者区分“候选没生成”“成本排错”“统计错误”和“运行时资源变化”四类问题。但强制执行备选计划也会增加实验成本，且很难完整覆盖生产中的并发与资源状态，因此 counterfactual 数据本身仍有采样偏差。

### 8.4 Benchmark 结论的外推边界

阅读实验时，还需要确认作者是否交代了：

- warm/cold cache；
- CPU frequency 与 NUMA binding；
- 存储队列深度；
- 并发查询背景流量；
- 编译/JIT 时间是否计入；
- materialization 与输出消费；
- 数据压缩和解码；
- 是否只测单 Join 而忽略上下游。

否则，所谓 crossover 只是某台机器、某次缓存状态下的偶然点。

---

## 9. 常见但危险的简化

### 9.1 “Hash 是 O(N)，所以默认 Hash”

忽略了 Hash Table 内存放大、随机访问、spill、skew 与 build side 误判。Hash 可以是很好的默认算法，但默认值来自真实 workload 的稳定性，不是只来自复杂度。

### 9.2 “Merge Join 是流式，所以内存总是小”

若输入无序，前置 Sort 本身阻塞且可能外排；重复键组也可能要求缓存。正确比较对象是完整属性路径，不是 Merge 内核。

### 9.3 “spillable 等于没有 OOM 风险”

spill 只解决可完成性。递归分区、磁盘竞争和最慢分区仍可能导致巨大 regret。

### 9.4 “NDV 足够表达 skew”

相同 NDV 可以有完全不同的 Top-K 频率。平均分布估计无法预测 hot partition、长 duplicate chain 和 `n:m` expansion。

### 9.5 “Cost 预测越准，计划一定越好”

优化器需要的是候选排序。应优先评价 pairwise accuracy、top-k coverage、P95/P99 regret，而不是只看绝对误差。

### 9.6 “实现更多算法一定更好”

每增加一种算法，都增加候选生成、属性推导、成本校准、语义测试、spill 与可观测性成本。Trino/Doris 的路线提醒我们：缩小搜索空间也可能是合理设计。新算法应证明它覆盖了现有路径无法稳定处理的区域。

---

## 10. 调研总结与开放问题

源码和论文并没有给出一套统一的 Hash/Sort 决策方法，反而展示了不同系统对问题边界的不同划分：CockroachDB 将属性搜索与分区级降级结合，TiDB 让有序路径和通用 Hash 路径承担不同责任，OceanBase 显式细分多类成本，而 Trino、Doris 将更多工程复杂度集中到 Hash、Distribution 和 spill。它们的差异不是某一条公式可以抹平的，而是工作负载、已有执行架构和维护成本共同作用的结果。

这次调研中，最值得保留的是三点认识：

1. **算法优劣必须在完整属性路径中比较。** 输入是否有序、是否需要 exchange、下游能否复用 Ordering，往往比 Hash 或 Merge 内核的微基准更重要。
2. **内存与 skew 改变的不只是常数，而是执行形态。** 从内存内执行到递归分区或多轮外排，成本存在明显的不连续性；平均 cardinality 和 NDV 很难表达这种风险。
3. **运行时自适应的价值来自可控退路，而不是随时改判。** 分区、物化 stage 和重复执行提供了不同的调整边界，但能否复用已完成工作、维持语义并限制失败代价，才是关键。

仍然没有简单答案的问题包括：

- 当 Ordering 的收益跨越多个算子时，局部成本比较能在多大程度上保留全局优势？
- 在并发和动态内存分配下，离线测得的 crossover surface 有多少仍然有效？
- 如何区分“统计误差导致选错”和“执行期间资源变化导致退化”，而不把两者都归因于 cost model？
- 鲁棒计划是否值得牺牲平均性能，取决于怎样的 SLO、负载分布和 regret 定义？
- 增加一个备选算法，何时真正扩大了稳定执行区域，何时只是增加搜索和维护成本？

因此，Hash 与 Sort 更适合被理解为两类数据重组机制，而不是一场等待永久赢家的算法竞赛。它们的边界随数据、物理属性、资源和硬件移动；本文的目的，是理解这些边界为何移动、各系统如何承担相应责任，以及现有研究还没有解决哪些问题。

---

## 参考源码

以下链接均固定到本次阅读的 commit，避免后续主干变更造成行号漂移。

- [CockroachDB - Join exploration rules](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/rules/join.opt#L253-L258)
- [CockroachDB - Hash/Merge Join cost](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/coster.go#L1062-L1136)
- [CockroachDB - External Hash Join and Sort-Merge fallback](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L24-L138)
- [CockroachDB - memo.Cost 与分层比较函数 Less](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/memo/cost.go#L17-L58)
- [TiDB - Physical Merge Join candidate generation](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L49-L124)
- [TiDB - Physical Stream Aggregate candidate generation](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_stream_agg.go#L39-L155)
- [TiDB - Hash/Merge Join cost V2](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/plan_cost_ver2.go#L737-L820)
- [TiDB - Hash Join V2 spill and restore](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L654-L767)
- [OceanBase - Hash/Merge Join cost](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L285-L430)
- [OceanBase - Sort cost variants](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L510-L576)
- [Trino - Join distribution selection](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/DetermineJoinDistributionType.java#L80-L169)
- [Trino - LocalCostEstimate 的三维成本结构](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/LocalCostEstimate.java#L33-L35)
- [Trino - Spilling Join execution](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/operator/join/spilling/SpillingJoinProcessor.java#L47-L155)
- [Apache Doris - Hash Join and Sort cost](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/CostModel.java#L257-L444)
- [Apache Doris - Cost 的三维结构与加权折算](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/Cost.java#L31-L52)
- [Apache Doris - Partitioned Hash Join spill path](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/pipeline/pipeline_fragment_context.cpp#L1761-L1810)

## 参考论文

- Goetz Graefe, 1993, [Query Evaluation Techniques for Large Databases](https://doi.org/10.1145/152610.152611)
- Yannis E. Ioannidis et al., 1992, [Parametric Query Optimization](https://www.vldb.org/dblp/db/conf/vldb/IoannidisNSS92.html)
- Navin Kabra, David J. DeWitt, 1998, [Efficient Mid-Query Re-Optimization of Sub-Optimal Query Execution Plans](https://www.vldb.org/dblp/db/conf/sigmod/KabraD98.html)
- Changkyu Kim et al., 2009, [Sort vs. Hash Revisited: Fast Join Implementation on Modern Multi-Core CPUs](https://www.vldb.org/pvldb/vol2/vldb09-257.pdf)
- Cagri Balkesen et al., 2013, [Multi-Core, Main-Memory Joins: Sort vs. Hash Revisited](https://www.vldb.org/pvldb/vol7/p85-balkesen.pdf)
- Maximilian Bandle et al., 2021, [To Partition, or Not to Partition, That is the Join Question in a Real System](https://db.in.tum.de/~bandle/papers/bandle-partitionVsNonPartition.pdf)
- Rong Zhu et al., 2023, [Lero: A Learning-to-Rank Query Optimizer](https://www.vldb.org/pvldb/vol16/p1466-zhu.pdf)
- Altan Birler et al., 2024, [Simple, Efficient, and Robust Hash Tables for Join Processing](https://db.in.tum.de/~birler/papers/hashtable.pdf)
- Haibo Xiu et al., 2024, [PARQO: Penalty-Aware Robust Plan Selection in Query Optimization](https://www.vldb.org/pvldb/vol17/p4627-xiu.pdf)
- Kevin P. Gaffney, Jignesh M. Patel, 2024, [Is Perfect Hashing Practical for OLAP Systems?](https://www.vldb.org/cidrdb/2024/is-perfect-hashing-practical-for-olap-systems.html)
- Riki Otaki et al., 2025, [Resource-Adaptive Query Execution with Paged Memory Management](https://www.vldb.org/cidrdb/papers/2025/p2-otaki.pdf)
- Paul Groß et al., 2025, [Adaptive Factorization Using Linear-Chained Hash Tables](https://www.vldb.org/cidrdb/papers/2025/p21-gro.pdf)
- Wentao Huang et al., 2026, [Hash Joins Meet CXL: A Fresh Look](https://www.vldb.org/cidrdb/2026/hash-joins-meet-cxl-a-fresh-look.html)

## 站内相关文章

本文讨论“选 Hash 还是选 Sort”，以下几篇分别从算子内部实现、优化器机制与资源维度补齐上下文：

- [深入 Hash Join：从内存哈希表到分区 Spill]({{< relref "2026-08-26-dive-hash-join.md" >}})：本文 §4.3 把 spill 当成一个相变点讨论，那篇把相变前后的哈希表结构与分区恢复拆开细看。
- [深入 Spark Hash Join：从 JoinSelection、HashedRelation 到 AQE 与 Spill 边界]({{< relref "2026-08-25-dive-spark-hash-join.md" >}})：单系统视角下的完整决策链，可与本文 §3.6 的五系统对比表对照阅读。
- [深入 Calcite VolcanoPlanner：Cost 账本、两种 RuleDriver 与 Trait Enforcement]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}})：本文 §2.3 说“Ordering 是可消费的物理属性”，那篇回答这个属性在 Cascades 搜索里到底如何被强制与传播。
- [查询并行度的三次决策：从初始分区到 AQE 与历史反馈]({{< relref "2026-09-12-dive-dop.md" >}})：本文 §6.1 把 stage 边界列为一种 adaptive barrier，那篇讨论在这些边界上还能调什么。
- [从芯片到数据中心：2026 AI Infra 与现代系统技术雷达]({{< relref "2026-09-10-system-ai-infra-hardware-distributed-systems.md" >}})：本文 §4.6 提到硬件会移动 crossover 边界，那篇把 CXL、带宽层级这些变量放到更大的图里。

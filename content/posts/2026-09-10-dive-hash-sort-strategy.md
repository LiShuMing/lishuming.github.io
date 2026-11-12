---
title: "【调研】Hash 还是 Sort：从优化器决策到运行时自适应"
date: 2026-09-10T00:00:00+08:00
lastmod: 2026-10-18T00:00:00+08:00
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
description: "从随机访存与顺序归并的硬件对偶性出发，结合 CockroachDB、TiDB、OceanBase、Trino 与 Apache Doris 源码及三十年论文脉络，拆解优化器与执行引擎如何在物理属性、内存悬崖、数据倾斜和运行时自适应之间做出选择。"
draft: false
toc: true
math: true
---

> 本系列第 2/3 篇 · Join 执行 · [上一篇]({{< relref "2026-08-26-dive-hash-join.md" >}}) · [下一篇]({{< relref "2026-08-24-dynamic-filter-outer-join.md" >}})

算法教科书上有一道看似毫无悬念的算术题：哈希表的单次查找期望复杂度是 `O(1)`，把 `N` 行数据连起来或按组聚合只需 `O(N)`；而基于比较的排序至少要付 `O(N log N)` 的代价。既然如此，数据库内核为什么不干脆把所有等值连接和分组聚合统统交给 Hash，还要大费周章地保留 Sort-Merge Join 和 Stream Aggregate？

如果我们用最朴素的方式在现代硬件上跑一条一亿行的连接查询，答案很快就会在两个极端场景里暴露出来。

假设内存宽裕、连接键分布均匀，Hash 确实快得惊人——它不在乎全序关系，只靠 `key → hash → bucket` 把相等的键聚拢到一起，省下了成千上万次键值比较。可一旦优化器把不同值个数（NDV）低估了十倍，或者遇到了几个拥有百万条记录的超级热点键，Hash 表就会撞上一连串物理瓶颈：首先是探测阶段的**随机内存跳跃**频繁击穿末级缓存（LLC）与 TLB，热点桶退化成长长的冲突链；紧接着，当哈希表体积逼近算子内存上限 `M` 时，系统不再是平滑变慢，而是发生剧烈的**内存溢写（Spill）相变**——原本一次过内存的探测被迫拆成“分区写盘 → 读回重建 → 二次探测”，要是热点键因为数值完全相同、怎么换哈希种子（Hash Seed）都切不小，递归哈希分区还会原地卡死。

反过来再看 Sort。它在启动阶段确实要付出 `O(N log N)` 的键归一化（Key Normalization）与归并代价，但它把杂乱的随机访存彻底改造成了**顺序读写与多路归并（k-way Merge）**：即便内存吃紧，外部归并排序也能按固定大小的有序顺串（Run）平稳落盘，退化斜率高度可预测；更重要的是，排序产出了一份可以顺流而下的物理资产——**有趣顺序（Interesting Ordering）**。

看一个日常报表里极其典型的四步流水线：先按用户 `k` 关联订单与行为表，再按 `k` 做分组汇总，最后在每个用户的窗口内按时间 `ts` 排序计算留存：

```text
路径 A（局部贪心选 Hash）：
  Scan → HashJoin(k) → HashAgg(k) → Sort(k, ts) → Window(k ORDER BY ts)
         [随机建表探测]   [再建一张哈希表]  [最后全量阻塞重排]

路径 B（前置一次顺序投资，下游连续免单）：
  OrderedScan(k, ts) → MergeJoin(k) → StreamAgg(k) → Window(k ORDER BY ts)
                       [顺序双指针归并]  [单组状态即算即吐] [直接复用已有顺序]
```

如果只盯着连接算子本身比快慢，路径 A 的 `HashJoin` 大概率赢过带排序负担的 `MergeJoin`；但把账算到整条流水线，路径 B 倘若能直接借用底层存储引擎按 `(k, ts)` 组织好的索引或微分区顺序，后面的 `MergeJoin`、`StreamAgg` 和 `Window` 几乎全是流式顺序扫描，峰值内存压到只有“当前正在处理的那一个分组”，把路径 A 里的两张大哈希表和最后一次全量 `Sort` 一笔勾销。

由此可见，“选 Hash 还是选 Sort”从来不是两个孤立算子的单挑，而是在**随机访存的分区能力**与**顺序归并的定序能力**之间算一笔端到端总账。下面我们顺着硬件与代价物理模型、五套工业级引擎（CockroachDB、TiDB、OceanBase、Trino、Apache Doris）的源码取舍、内存与倾斜引发的交叉点漂移，以及运行时自适应降级与三十年研究演进，把这笔账彻底算清楚。

---

## 一、硬件与算法对偶：为什么 `O(N)` 与 `O(N log N)` 算不出真实胜负

在真实的执行引擎里，Hash 和 Sort 甚至不是互斥的阵营，而是两种可以自由嵌套的数据重组原语：**Hash 负责按键分桶（Partitioning），把大问题切成互不相干的独立子集；Sort 负责按键定序（Ordering），把同键元组在空间上紧挨着排成顺串。**

翻开任何一个支持落盘的现代算子，都能看到两者的混搭：Grace Hash Join 先用 Hash 把左右输入切成几十个磁盘分区，再对能塞进内存的小分区建哈希表；分布式 Merge Join 往往要先靠 Hash Shuffle 把相同键发往同一个计算节点，再在节点内部做局部排序归并；更有甚者，在 CockroachDB 的外部哈希连接（[`external_hash_joiner.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L24-L55)）里，同一个算子跑着跑着还会自己“叛变”——先按哈希函数递归切分落盘，一旦发现某个分区换了哈希种子也缩不小（说明撞上了大量重复键），就干脆只把这一个坏分区交给磁盘外排加 Merge Join 处理，其余健康分区照常走内存 Hash Join。

既然底层原语都在互相借力，评价一条等值连接路径就不能只抄大 `O` 符号，而要把跨网络交换（Exchange）、溢写风险期望与下游顺序复用全都塞进成本方程里：

$$
C_{\text{hash\_join}} = C_{\text{build}} + C_{\text{probe}} + C_{\text{exchange}} + \mathbb{E}[C_{\text{spill}}]
$$

$$
C_{\text{merge\_join}} = C_{\text{sort}}(L) + C_{\text{sort}}(R) + C_{\text{merge}} + C_{\text{exchange}} - B_{\text{order\_reuse}}
$$

把这两条管线的内存布局与外存 I/O 轨迹并排摊开，就能直观看到常数项与非线性拐点究竟长在哪里：

![Hash 与 Sort 的内存访存及外存 I/O 执行管线对偶对比](/images/diagrams/exec-hash-vs-sort-mechanics.svg)

- **访存局部性与内存放大**：Hash Join 的建表阶段不仅要存键值和有效载荷（Payload），还要承担桶目录负载因子（Load Factor）、冲突链指针以及内存分配器对齐开销，实际占用的字节数往往是原始列宽的好几倍；一旦哈希目录溢出 CPU 的 L3 缓存，每次探测都会退化成高延迟的随机 DRAM 访问。而现代排序早已不是教科书里逐字段调用虚函数比较器的慢速快排——借助前缀键归一化（把多列拼成定长无符号整数按 `memcmp` 或寄存器直接比较）、基数排序（Radix Sort）与 SIMD 排序网络，顺序带宽利用率极高。
- **下游属性复利（`B_order_reuse`）**：Hash 产出的哈希表是一次性消费品，连接做完就得销毁（最多把分桶分布属性传给下游同键算子免除一次 Shuffle）；而 Sort 产出的有序性可以沿算子树一路向上传递，连续省掉后续聚合、窗口或最终 `ORDER BY` 的重排开销。
- **首行延迟与流水线阻塞边界**：“Merge Join 是流式算子、Hash Join 是阻塞算子”是另一个流传甚广的片面印象。算子能不能边读边吐、快速响应 `LIMIT 10`，取决于它吐出第一行结果前必须攒够多少状态：

| 物理算子 | 吐出首行前的必要阻塞状态 | 容易踩坑的边界条件 |
| --- | --- | --- |
| **Hash Join** | 必须完整消费完 Build 侧并建好哈希表 | Probe 侧可逐批流式穿过；但 `RIGHT` / `FULL OUTER JOIN` 若需最后扫描未匹配 Build 行，尾阶段仍会停顿 |
| **Merge Join** | 左右两侧输入必须已按连接键有序到达 | 若上游挂着全量 `Sort`，整条链路照样被阻塞；且遇到多对多重复键组时需在内存或磁盘缓冲右侧匹配段 |
| **Hash Aggregate** | 通常需攒齐全部输入以维护完整分组表 | 局部预聚合（Streaming Pre-Aggregation）遇到高基数键无法有效减量时，可直接退化为透传以止损 |
| **Stream Aggregate** | 输入已按分组键连续聚集，仅保留当前单组状态 | 内存占用极小且一结组就向下游吐行，但强依赖上游提供严格分组序 |
| **Sort 家族** | 传统全排需读完全部输入才能吐出最小行 | 若带 `LIMIT K`（Top-N 堆）、或上游已有前缀序（Prefix / Incremental Sort），启动延迟与内存都会骤降 |

正因为首行耗时、峰值内存与总吞吐的权重随场景剧变，交互式点查（`LIMIT 10`）与夜间全量宽表 ETL 面对同一组输入统计，完全可能挑出截然相反的赢家。

---

## 二、优化器的三维棋盘：算法、数据分布与有趣顺序

走进分布式或并行数据库的代价优化器（CBO），你会发现它从来不会孤立地问“这步用 Hash 还是 Merge”，而是在一个由**本地算法、数据分布（Distribution）与排序属性（Ordering）**张成的三维空间里联合选型：

```text
JoinCandidate {
    algorithm:    HASH | MERGE | LOOKUP | NESTED_LOOP
    build_side:   LEFT | RIGHT | NONE
    distribution: COLOCATED | BROADCAST | HASH_SHUFFLE | RANGE_SHUFFLE
    ordering:     required / provided sort prefix & collation
    runtime_mode: IN_MEMORY | SPILLABLE | ADAPTIVE
}
```

为什么要把这几个维度绑在一起搜？设想一张 50 MiB 的维表关联一张 500 GiB 的事实表。如果走广播哈希连接（Broadcast Hash Join），事实表一行都不用过网卡，每个工作节点各自在本地内存建一张 50 MiB 的小哈希表就能流式跑完；要是换成重分区连接（Partitioned Hash Join 或分布式 Merge Join），500 GiB 事实表瞬间就要全量序列化塞进网络做 Shuffle——省掉一次全表网络交换的收益，比你在本地哈希探测循环里抠掉十条 CPU 指令大出好几个数量级。反过来，如果 Build 侧涨到了 20 GiB，继续在 32 个节点上做广播，集群总内存消耗就会平白放大 32 倍，直接把节点打进 OOM。

在放进代价公式打分之前，优化器还必须先过一道硬性的**语义准入筛子**，把根本算不对结果的非法组合剔除出局：

1. **相等性与排序规则语义**：连接条件里有没有 `NULL`-safe 等值（`<=>` 或 `IS NOT DISTINCT FROM`）？左右两侧字符串列的字符集排序规则（Collation）是否一致？如果排序器的大小写/口音折叠规则与等值判定不一致，Merge Join 按字典序归并就会漏匹配。
2. **连接方向与复制合法性**：假设做的是 `A RIGHT JOIN B`，且执行引擎固定拿右侧 `B` 建哈希表。此时**绝不允许把 `B` 广播复制到多台机器上**——因为每台机器只拥有左表 `A` 的一部分分片，某行 `b` 在节点 1 没配对成功，不代表它在节点 2 也没配对成功；一旦各节点各自输出“未匹配的右表行”，最终结果就会冒出成堆幽灵重复行。

跨过语义准入后，优化器内部（尤其是基于 Cascades / Volcano 框架的 Memo 搜索器，详见[《深入 Calcite VolcanoPlanner：Cost 账本、两种 RuleDriver 与 Trait Enforcement》]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}})）靠**属性生产与消费机制**来算清前文提到的“跨算子复利”。在 Memo 组里，每一个等价逻辑表达式不仅记录“不要求任何顺序时的最便宜计划”，还会按上层索要的物理属性（Required Property）分开建账：Sort 和 Exchange 扮演**属性强制器（Enforcer）**，负责花钱制造顺序或分区；Merge Join 和 Stream Aggregate 则扮演**属性消费者**，负责把这份投资兑现成更低的算子成本。只有把 `Ordering` 和 `Distribution` 留在 Memo 状态签名里，局部略贵、全局更省的有序流水线才不会被贪心剪枝早早误杀。

---

## 三、五套工业引擎的工程分野：把复杂度预算花在哪里？

虽然理论棋盘是同一个，但打开主流开源数据库的源码，会看到两条泾渭分明的工程路线：**CockroachDB、TiDB 和 OceanBase** 把 Ordering 属性和 Merge/Stream 算子完整纳入优化器竞争；而 **Trino 和 Apache Doris** 等现代向量化 MPP 引擎在通用等值连接路径上干脆不生成 Sort-Merge Join，把全部工程筹码压在了数据分布选型、Runtime Filter 与分区化 Hash Spill 上。

### 3.1 全路径竞争派：CockroachDB、TiDB 与 OceanBase

先看 **CockroachDB**（基线 `8812064`）。它的探索规则 [`GenerateMergeJoins`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/rules/join.opt#L253-L258) 并不是见到等值连接就无脑塞一个双侧全排的 Merge Join，而是紧盯输入端能够提供的有趣顺序（Interesting Ordering）来派生候选。到了代价估算器 [`coster.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/coster.go#L1062-L1136) 里，两条路径的不对称开销写得非常直白：

```go
// Hash Join：右侧固定作为 Build 侧写入哈希表，赋予更高的 CPU 系数并叠加行缓冲开销
cost := memo.Cost{C: (1.25*leftRowCount + 1.75*rightRowCount) * cpuCostFactor}
cost.Add(c.rowBufferCost(rightRowCount))

// Merge Join：向量化流水线下左侧近似流式读取，右侧可能因重复键组产生局部缓冲
cost := memo.Cost{C: (0.9*leftRowCount + 1.1*rightRowCount) * cpuCostFactor}
```

这段代码上方还藏着一处极易被忽略的细节：当连接类型是 `Semi` 或 `Anti` 且 `leftRowCount < rightRowCount` 时，计价函数会悄悄把左右行数对调。原因在于 CockroachDB 执行器总是拿右表建哈希表，计划构建器（`execbuilder`）在落地时会自动把更小的一侧换到右边，但优化器代数里偏偏没有定义 `Right Semi/Anti Join` 节点——如果计价函数不提前模拟这次换边，优化器就会误以为大表进了哈希表，把好计划的成本算反。而在执行兜底上，前文提到的 [`external_hash_joiner.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L83-L138) 给出了漂亮的分区级退路：正常分区走 Grace Hash，唯独递归哈希切不开的倾斜分区才单独拉起磁盘外排器转走 Merge Join，决不牵连整条查询重头再来。

**TiDB**（基线 `fe7ae36`）同样围绕顺序属性做精细准入。在 [`physical_merge_join.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L49-L124) 与属性推导逻辑（[`L425-L453`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L425-L453)）中，优化器会逐项核验连接键是否完整命中子节点的排序前缀、`ENUM/SET` 类型与字符集规则是否兼容、是否含暂不支持的 `NULL-safe` 比较；[`physical_stream_agg.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_stream_agg.go#L39-L155) 也严格区分“天然顺路消费子节点顺序”的免排候选与“主动要求子节点补排分组键”的强制候选，并在 [`plan_cost_ver2.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/plan_cost_ver2.go#L737-L820) 中结合行宽、键数、内存与并发因子计价。

而在执行端，TiDB 的 [`hash_join_v2.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L654-L767) 没有切去排序，而是把分区化落盘恢复（[`L1103-L1164`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L1103-L1164)）打磨得很扎实：向 `MemTracker` 注册 `spillAction` 回调，一旦内存告急就把选定分区刷盘，并让探测端对应分区的行一并落盘，随后从 `spillHelper.stack` 栈里逐个弹出分区重建探测。最讲究的是它的终止条件 `initMaxSpillRound`——按 `log(1024) / log(partitionNumber)` 计算最大递归轮数，也就是**锁死累计分区总数不超过 1,024**。因为真正决定磁盘随机 I/O 放大和文件句柄开销的是裂变出来的总分区个数，而不是抽象的递归层数。

**OceanBase**（基线 `ae69064`）则展示了如何把白盒代价模型拆到足够可解释的粒度。翻开 [`ob_opt_est_cost_model.cpp`](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L285-L430)，`cost_hashjoin` 逐项累加 Build 物化、哈希计算、桶插入、Probe 哈希、查表、Join Filter 构建与过滤、残余谓词及输出成本，`cost_mergejoin` 与分组聚合成本（[`L1125-L1187`](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L1125-L1187)）也各自独立建模。尤其精彩的是排序计价入口 [`cost_sort`](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L510-L576) 里那串严格保序的六路分支：

1. `is_local_merge_sort_`（局部归并排序）
2. `prefix_pos_ > 0`（利用已有前缀的增量排序 Prefix Sort）
3. `part_cnt_ > 0 && topn_ >= 0`（窗口分区内 Top-N）
4. `topn_ >= 0`（全局 Top-N 堆排序）
5. `part_cnt_ > 0`（分区排序）
6. `Normal Sort`（兜底全量外排）

为什么要排得这么细？试想如果优化器偷懒，把所有 `Sort` 统统按全量 `N log N` 计价，那么原本只需维护小顶堆的 `Top-N`、或者只需在相同前缀组内微调的 `Prefix Sort` 就会被严重高估；一旦前置排序被高估，下游一整串依赖顺序的 `Merge Join` 与 `Stream Group` 都会跟着被连坐淘汰。

### 3.2 收缩搜索空间派：Trino 与 Apache Doris

换到 **Trino**（基线 `68dae09`）和 **Apache Doris**（基线 `5202d06`），画风立刻变了：你在通用连接规则里根本找不到 Sort-Merge Join 的身影。这不是“漏写了算法”，而是一笔深思熟虑的工程账——少维护一套连接与外排融合内核，把省下来的复杂度全投进分布式 Shuffle 选型、动态过滤（Dynamic/Runtime Filter）与向量化哈希表特化上。

在 Trino 的 [`DetermineJoinDistributionType.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/DetermineJoinDistributionType.java#L80-L169) 中，核心博弈是“左右谁做 Build 侧”乘以“`REPLICATED`（广播）还是 `PARTITIONED`（重分区）”。它先用 `mustPartition` 把 `RIGHT` / `FULL` 连接的广播选项直接封死（防止前面说的右表未匹配行重复输出）；等遇到统计信息缺失、只能退回 `getSizeBasedJoin` 靠已知字节数盲猜时，代码设了一个耐人寻味的阈值 `SIZE_DIFFERENCE_THRESHOLD = 8`——因为残缺统计可能漏算了上游过滤或聚合的缩减率，**只有当一侧比另一侧小足足 8 倍以上、误差再大也不可能颠倒大小关系时，才敢动手翻转 Build 侧**。到了本地物理规划阶段（[`LocalExecutionPlanner.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/LocalExecutionPlanner.java#L2869-L2983) 与 [`SpillingJoinProcessor.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/operator/join/spilling/SpillingJoinProcessor.java#L47-L155)），落盘开关除了检查会话配置，还严卡 `!buildOuter` 且要求固定并发度（`getDriverInstanceCount` 存在），因为一旦 Build 侧需要追踪外连接未匹配标志位，跨轮次落盘恢复极易破坏状态一致性。

Doris 的 Nereids 优化器同样聚焦在连接次序、分布方式（Colocate / Bucket Shuffle / Broadcast / Shuffle）与 Runtime Filter 穿透力上。翻看 [`CostModel.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/CostModel.java#L257-L444) 里的 `visitPhysicalHashJoin`，会看到一段非常务实的“微扰破平局”代码：

- 当左右估计行数打平且统计不可信时，比较连接图连通度 `computeConnectivity`，给较弱一侧加上 `leftRowCount += 1`；
- 若连通度也相同，再比元组字节宽度，给把宽行放进 Build 侧的方案加上 `leftRowCount += 1e-3`；
- 若单表 Build 侧能产出高效的 Runtime Filter，则直接从 `leftRowCount` 里扣掉一笔 `bonus`，诱导优化器优先拼出能尽早裁剪大表的连接次序。

你看，Doris 根本没假装把连通度或行宽折算成精准的纳秒数，而是用 `1` 与 `1e-3` 的数量级差直接编码了启发式优先级——既然优化器最终只看谁排在前面，诚实地控制相对次序远比捏造虚假的绝对耗时管用。在后端执行器 [`pipeline_fragment_context.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/pipeline/pipeline_fragment_context.cpp#L1761-L1810) 中，只有非广播且允许落盘的连接才会实例化 `PartitionedHashJoinProbeOperatorX` / `SinkOperatorX`；至于 Broadcast Join，既然每个实例都要持有全量右表，切分区落盘省不下多少内存，Doris 选择用 `enable_share_hash_table_for_broadcast_join` 让同节点多实例共享同一张内存哈希表来化解压力。

把五套系统的职责切分摆在一起，就能看清两派各自的得失：

| 数据库系统 | 优化期主博弈空间 | 重点追踪的物理属性 | 执行期兜底武器 | 架构取舍的代价 |
| --- | --- | --- | --- | --- |
| **CockroachDB** | Hash / Merge / Lookup 全面竞争 | `Ordering` 深入 Memo 状态 | Grace Hash + 坏分区转外排 Merge | 经验成本系数仍需随算子演进调校 |
| **TiDB** | Hash / Merge / Index 连接；Hash / Stream 聚合 | `Ordering` 前缀与字符集准入 | `HashJoinV2` 分区落盘（1,024 分区封顶） | 优化期对潜在落盘轮数的预判仍较粗略 |
| **OceanBase** | 细粒度拆解 Join / 六种 Sort / Group 成本 | `Ordering` 与 `Distribution` 并重 | 自适应工作区（Work Area）与外存执行 | 白盒公式分支繁多，跨硬件校准门槛高 |
| **Trino** | 左右翻转 × `REPLICATED` / `PARTITIONED` | `Distribution` 与外连接合法性 | 分区化 `SpillingJoinProcessor`（禁 `buildOuter`） | 放弃双侧有序直连红利与排序内存稳定性 |
| **Apache Doris** | 连接次序 + 四种分布 + Runtime Filter | `Distribution` 与过滤连通度 | 分区 Hash Spill + 广播共享哈希表 | 排序算子独立服务语义，较少参与连接属性接力 |

---

## 四、移动的交叉点：内存相变、数据倾斜与成本账本的盲区

为什么明明在实验室里标定好的成本系数，一上生产环境还是会频繁选错？因为 Hash 与 Sort 的胜负交叉点（Crossover）根本不是固定在“某几百万行”的一个点，而是一张随**真实内存字节、键分布偏斜度与运行时配额**剧烈起伏的曲面。

### 4.1 内存悬崖与长尾倾斜：平均值掩盖了什么

第一个容易把只看行数（Cardinality）的优化器坑惨的变量，是**哈希表的真实物理体积与内存悬崖**：

```text
hash_table_bytes ≈ directory_bytes(load_factor)
                 + distinct_key_bytes
                 + payload_rows × payload_width
                 + duplicate_chain_or_adjacency_bytes
                 + allocator_and_alignment_overhead
```

哪怕两个子计划输出的 `build_rows` 一模一样，512 字节宽的变长字符串行与 8 字节定长整数行占用的内存天差地别；低基数（低 NDV）在分组聚合里能压缩状态，但在必须保留全部匹配行载荷的 Hash Join 里，成千上万条同键记录照样得全塞进内存挂成链表或邻接数组。

一旦 `hash_table_bytes` 顶穿当前算子分到的有效内存预算 `M`，代价曲线就会出现断崖式跳变：在 `bytes < M` 时它是微秒级纯内存流水，越过 `bytes ≈ M` 的瞬间就被迫付出整轮磁盘读写（`partition → write → read → rebuild → reprobe`）。而在高并发共享集群里，单条查询能拿到的 `M` 本身就在随邻居负载实时波动——编译期写死一个常数内存假设，在内存水位线附近几乎必然踩雷。

第二个隐形杀手是**数据倾斜（Data Skew）与网络搬运**。分布式 Stage 的完工时间从来不取决于平均每个分区有多少行，而取决于最慢的那个热点桶：

$$
T_{\text{stage}} \approx \max_{1 \le i \le p} T(\text{partition}_i) + T_{\text{coordination}}
$$

只存总行数和总 NDV 的统计直方图看不到重尾（Heavy Hitters）：同样是 1 亿行、100 万个不同键，均匀分布时每个分区轻快跑完；要是某一个默认空值或爆款商品独占了 3,000 万行，Hash Shuffle 不仅会把这 3,000 万行全砸向同一个倒霉的下游节点引发单点落盘，还会在多对多连接（`n:m`）时瞬间炸出天文数字般的乘积输出。相比之下，基于采样的范围切分（Range Partitioning）与归并虽然多一步边界探测，却能更直观地看清值域跨度并拆解热点区间。再加上硬件代际更迭——AVX-512 宽向量加速基数排序、NUMA 跨插槽访存惩罚，或是 CXL 远端内存池的延迟分层（参见[《从芯片到数据中心：2026 AI Infra 与现代系统技术雷达》]({{< relref "2026-09-10-system-ai-infra-hardware-distributed-systems.md" >}})）——都在不断改写两条路径的底层斜率。

### 4.2 成本向量的折叠时机与风险定价

既然影响交叉点的维度这么多，现有数据库的成本数据结构接得住吗？翻开前面五个引擎的 `Cost` 定义，会看到一幅很有意思的演进图景——分歧不在“算不算 CPU、内存和网络分项”，而在**“分项在哪一步被拍扁成一个标量浮点数”**：

- **Doris（构造即折叠）**：[`Cost.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/Cost.java#L31-L52) 虽然持有 `cpuCost`、`memoryCost`、`networkCost` 三个字段，但在构造函数里就立刻乘上会话权重加总成了单一 `cost` 标量参与 Memo 淘汰。
- **Trino（延迟到比较器折叠）**：[`LocalCostEstimate.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/LocalCostEstimate.java#L33-L35) 把三维分项一路保留到 [`CostComparator.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/CostComparator.java#L54-L71) 的 `compare` 方法里，先用 `checkArgument` 拒绝拿含未知维度的残缺成本瞎比，最后才做加权求和。更有意思的是源码里留下的那行 `TODO`：
  ```java
  // TODO when one left.getMaxMemory() and right.getMaxMemory() exceeds query memory limit
  //      * configurable safety margin, choose the plan with lower memory usage
  ```
  这句话一针见血地道出了线性加权和的软肋：一个 CPU 极便宜但内存超限的 Hash 计划，加权总分依然可能压过稳健的备选方案，结果一跑起来就直接落盘甚至 OOM！正因为 Trino 把分项留到了比较前一刻，它才看得到 `maxMemory` 去动这把手术刀。
- **TiDB（标量比大小，旁路留账本）**：[`cost_misc.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/util/costusage/cost_misc.go#L49-L68) 里的 `CostVer2` 用单一 `float64` 参与竞选，同时挂着一个 `CostTrace` 记录各因子明细与可读公式（如 `hashmem(rows*size*factor)`），专门服务于 `EXPLAIN ANALYZE` 排障与离线成本校准。
- **CockroachDB（位掩码字典序优先）**：[`memo/cost.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/memo/cost.go#L17-L58) 在浮点数 `C` 之上垫了一个 `uint8` 的 `Penalties` 位掩码（涵盖强制 Hint、全表扫描惩罚、无界基数惩罚等），`Less` 函数先比掩码高低，同级之内再用 `ulpTolerance = 1000`（相对浮点精度的容差）比大小，彻底杜绝了“给危险计划硬编一个 `1e100` 魔数”导致的浮点溢出与精度丢失。

然而，哪怕把这五套实现最精细的部分拼起来，离理想的风险感知决策仍然缺了四块关键拼图：**预期溢写字节与轮数（`expected_spill_bytes / rounds`）、启动首行耗时（`startup_ns`）、下游属性变现净值，以及基数估计本身的置信度（`confidence`）**。

这也正是近两年优化器前沿研究发力的靶心：

- **给不确定性定风险价**：PVLDB 2024 的 **[PARQO](https://www.vldb.org/pvldb/vol17/p4627-xiu.pdf)** 不再假装基数点估计是真值，而是引入选择率误差分布，用候选计划 `p` 在不同真实状态 `s` 下的期望后悔惩罚（Expected Penalty）与尾部风险来选型：
  $$
  \operatorname{RiskAwareScore}(p) = \mathbb{E}_s[\operatorname{Cost}(p, s)] + \lambda \cdot \operatorname{TailRisk}\!\left(\operatorname{Cost}(p, s) - \min_q \operatorname{Cost}(q, s)\right)
  $$
  通过敏感性分析锁定最致命的两三个变量（比如 `build_bytes` 与可用内存之比），就能识别出那种“估准了快 15%、一旦估低两倍落盘就慢 10 倍”的脆弱 Hash 计划，在延迟敏感场景下主动换用退化曲线更平缓的路径。
- **盯住成对排序与后悔率，而非拟合绝对秒数**：PVLDB 2023 的 **[Lero](https://www.vldb.org/pvldb/vol16/p1466-zhu.pdf)** 点破了另一个度量误区——哪怕成本模型预测耗时的平均百分比误差（MAPE）很低，只要在 Hash 与 Sort 的交叉点附近把冠亚军排反了，照样选出烂计划；反过来，只要成对排序准确率（Pairwise Accuracy）高、最坏后悔比 \(\operatorname{Regret} = \frac{T(\text{chosen})}{T(\text{best})}\) 受控，绝对数值差十倍也无伤大雅。

---

## 五、运行时自适应：在状态边界上安全换挡

哪怕优化器引入了风险模型，面对包含复杂 UDF、多表相关过滤的陌生查询，编译期照样可能看走眼。这时候压力就传到了执行引擎手里：**跑了一半发现不对劲，还能不能反悔？**

答案是能，但绝不是“直接把建了一半的哈希表扔掉重排”那么鲁莽。在流式管道里随意重启算子，不仅白白烧掉已完成的 CPU 与 I/O，一旦上游已经向客户端吐出过部分连接结果，或者算子内部正维护着 `LEFT/RIGHT/ANTI JOIN` 的“某行是否匹配过”状态位，贸然切换就会立刻酿成重复输出或丢行错数。

因此，工业界真正落地的运行时自适应，全都严守着**三条天然的状态隔离边界（Adaptive Barriers）**：

1. **算子内部的独立分区与微批边界**：这是粒度最细、止损最精准的一道防线。无论是连接算子触碰内存配额，还是局部预聚合遭遇高基数输入导致缩减率暴跌，执行器都只在干净的状态隔离点上切换策略——前面拆解过的 CockroachDB `ExternalHashJoiner` 之所以敢在运行中从 Hash 切到 Sort-Merge，正是因为首轮哈希落盘已经把左右输入按键值严格隔离成了互不相干的分区对（Partition Pair）：

![CBO 编译期选型与运行时状态边界自适应降级](/images/diagrams/exec-hash-sort-adaptive-fallback.svg)

注意这里切换的触发判据不是单纯“内存不够”，而是**“继续哈希递归已经失去缩小分区的进展”**；并且切换范围被死死关在尚未输出任何结果的单个坏分区笼子里，外连接标记与已完成分区毫发无损。类似的思路也体现在流式预聚合（Streaming Pre-Aggregation）里：先在 L1/L2 缓存大小的微型哈希表里攒批聚合，一旦实时监测到分组消减率（Reduction Ratio）低于阈值，立刻把当前攒好的批次刷向下游，随后直接切为无状态透传，绝不在高基数输入上硬撑一张注定溢出的大表。

2. **跨阶段（Stage / Shuffle）物化边界**：在 Spark AQE、Flink Adaptive Batch 或 Trino 容错执行模式下（详见姊妹篇[《查询并行度的三次决策：从初始分区到 AQE 与历史反馈》]({{< relref "2026-09-12-dive-dop.md" >}})），上游 Shuffle 写盘结束的那一刻，真实的分区字节分布、最大桶体积与行数全部水落石出，而下游消费算子尚未拉起。这时把原本规划的 Shuffle Hash / Sort-Merge Join 动态降级成 Broadcast Hash Join，或者把倾斜的热点桶拆成多段范围并行处理，既零浪费地复用了上游物化成果，又绕开了算子半途变轨的状态泥潭。

3. **跨执行的查询画像边界**：对于每天成百上千次跑同一模板的报表或微服务 SQL，执行器在运行期攒下的结构化遥测指标——真实 `build_bytes`、最大桶频率、落盘轮数（`spill_rounds`）、外排归并趟数、以及下游算子是否真的消费了排序属性——只要绑定好“规范化子计划签名 + 数据快照代际 + 硬件规格”，就能在下一次编译时直接把优化器拉回正确轨道。

更进一步看，算法自适应底下其实还垫着一层更基础的**资源自适应**。如果同一台机器上并发跑着四条查询，每条算子都死守着编译期静态切分的内存配额，就会出现荒诞的一幕：左边查询差 50 MiB 内存就能避开整轮磁盘落盘，右边查询却抱着 2 GiB 空闲配额在慢悠悠等网络。CIDR 2025 的 **[Paged Memory Management](https://www.vldb.org/cidrdb/papers/2025/p2-otaki.pdf)** 把 Sort、Hash Table 和 Aggregate 的工作区统一收归页式内存池调度，支持按边际收益在算子间动态腾挪内存甚至做轻量级查询上下文切换——当可用内存 `M` 本身能按需伸缩时，很多原本处于溢写悬崖边缘的 Hash 算子根本不用走到落盘那一步。

---

## 六、三十年学术脉络与实验验证标尺

回头串联过去三十多年的数据库顶会文献，会发现学术界围绕“Hash 还是 Sort”的追问，早已走过了“争论谁是宇宙第一”的青涩期，演进了整整四代问题意识：

- **第一代：外存 I/O 模型与参数化规划（1990s）**。Graefe 在 1993 年的经典综述 [《Query Evaluation Techniques for Large Databases》](https://doi.org/10.1145/152610.152611) 奠定了沿用至今的对偶定理：外部归并排序的分段顺串生成与多路合并，在数学结构上同构于哈希连接的分区裂变与逐桶探测，内存与输入之比决定了读写趟数。几乎同一时期，Ioannidis 等人的 [参数化查询优化（PQO，1992）](https://www.vldb.org/dblp/db/conf/vldb/IoannidisNSS92.html) 指出可用缓冲池大小在编译期未知，应当预备覆盖不同参数区间的计划集；Kabra 与 DeWitt 的 [执行期重优化（1998）](https://www.vldb.org/dblp/db/conf/sigmod/KabraD98.html) 则首开在物化点收集统计修正后续计划的先河。
- **第二代：多核 SIMD 与内存墙之争（2009–2013）**。当大容量内存成为主流，瓶颈从磁盘磁头挪到了 CPU 缓存行与寄存器宽度。2009 年 Kim 等人在 [《Sort vs. Hash Revisited》](https://www.vldb.org/pvldb/vol2/vldb09-257.pdf) 中提出更宽的 SIMD 向量将助推 Sort-Merge 反超；紧接着 2013 年 Balkesen 等人在 [《Multi-Core, Main-Memory Joins》](https://www.vldb.org/pvldb/vol7/p85-balkesen.pdf) 中在同一硬件平台上把两派都压榨到极致，测出多趟基数分区哈希（Radix Hash Join）在绝大多数规模下依然领先，Sort-Merge 仅在超大数据集上逼近。这场论战最大的遗产是证明了**微架构常数（TLB 页大小、NUMA 拓扑、SIMD 宽度）会实实在在移动交叉点**。
- **第三代：走出孤立微基准，算全链路真账（2021）**。微基准里的冠军放进完整 SQL 引擎里还是冠军吗？2021 年慕尼黑工业大学团队在 Umbra 引擎中做了著名的对照实验 [《To Partition, or Not to Partition, That is the Join Question in a Real System》](https://db.in.tum.de/~bandle/papers/bandle-partitionVsNonPartition.pdf)：在单算子窄列测试里大杀四方的 Radix Partitioned Join，一放进完整的 TPC-H 流水线就频频吃瘪——因为提前按基数做多趟物理分区，等于强迫 CPU 把所有输入元组（包括那些在探测时根本连不上、本该被 Bloom Filter 一眼筛掉的废数据，以及宽重载荷列）统统搬运物化了好几遍！反倒是不预分区的哈希连接配合过滤穿透在全链路上表现更稳。
- **第四代：数据结构自免疫、运行时采样与异构内存（2024–2026）**。最近三年，研究重心彻底下沉到了消灭最坏情况退化：DaMoN 2024 的 **[Unchained Hash Table](https://db.in.tum.de/~birler/papers/hashtable.pdf)** 用分区构建 + 连续邻接数组 + 流水化探测 + Bloom Filter 兼顾主外键快路径与高重复键倾斜；CIDR 2024 的 **[Perfect Hashing](https://www.vldb.org/cidrdb/2024/is-perfect-hashing-practical-for-olap-systems.html)** 评估了完美哈希在 OLAP 密集查表中的构建与探测平衡；CIDR 2025 的 **[Adaptive Factorization](https://www.vldb.org/cidrdb/papers/2025/p21-gro.pdf)** 利用 Hash Build 阶段顺手构建轻量级度数草图（Sketch），在运行时动态拍板要不要启用因子化压缩以躲避多对多展开爆炸（详见[《深入 Hash Join：从内存哈希表到分区 Spill》]({{< relref "2026-08-26-dive-hash-join.md" >}})及 [Spark 附录]({{< relref "2026-08-26-dive-hash-join.md" >}}#spark-hash-join)）；而 CIDR 2026 的 **[Hash Joins Meet CXL](https://www.vldb.org/cidrdb/2026/hash-joins-meet-cxl-a-fresh-look.html)** 则揭示在 DRAM + CXL 分层内存上，盲目把数据全搬回本地 DRAM 未必划算，按访存热度做部分迁移才能榨干双侧总带宽。

把源码与这三十年的实验教训浓缩成日常内核研发的排查准则，最管用的是在动手做性能评测或改优化器规则前，先拿下面这把标尺过一遍常见的认知陷阱：

| 似是而非的经验口诀 | 真实物理机制下的翻车现场 | 正确的验证与设计姿势 |
| --- | --- | --- |
| **“Hash 是 `O(N)`，闭眼无脑选 Hash”** | 撞上宽行内存放大、Build/Probe 估反或重尾热点键时，随机访存失速叠加多轮落盘，延迟瞬间劣化十倍 | 联合考察 `build_bytes / M` 水位、Top-K 频率与外连接语义，测绘多维交叉曲面而非单条曲线 |
| **“Merge Join 流式低内存，不怕 OOM”** | 若输入原本无序，前置全量 `Sort` 既阻塞又照样外排；且右侧遇到百万级同键组仍需缓冲回退 | 把上游 `Enforcer` 排序费和下游顺序复用红利绑成整棵子树一起算账 |
| **“算子支持 Spill 就绝不会挂”** | 外连接未匹配追踪可能直接禁掉 Spill（如 Trino），且最小工作页缓冲、1,024 分区句柄与并发磁盘争抢仍有硬底线 | 在 `M / input_bytes` 为 `0.1 / 0.25 / 0.5 / 1.0` 多档压力下实测落盘轮数与尾延迟 |
| **“只要把 Cost 耗时拟合准了就行”** | 绝对误差再小，只要交叉点附近冠亚军排反照样选烂计划；且线上日志只录了当选计划，缺失备选反事实样本 | 在相同快照与配额下强制固定 Hash / Merge / Broadcast 跑反事实对照，盯紧 Pairwise 排序率与 P99 Regret |
| **“内核塞进的算法越多越先进”** | 每多一套算子，规则枚举、属性传递、Spill 测试矩阵和校准面就翻一倍（对比 Trino/Doris 的精简路线） | 只有当新算法能稳稳覆盖现有路径搞不定的盲区（如已有序流或极端倾斜兜底）时才值得入列 |

说到底，现代查询引擎早已不再指望优化器在查询启动前的那几毫秒里扮演全知全能的先知。最稳健的系统架构，总是让**优化器算清跨算子的物理属性复利与风险边界，让哈希表与排序内核各自收窄最坏退化幅度，再在分区、Stage 与历史反馈三道关卡上留好体面的退路**——哪怕统计信息偶尔撒了谎，查询依然能稳稳当当地跑过终点线。

---

## 参考资料与源码基线

> **源码基线说明**：文中五个开源系统的实现细节与行号均固定在以下阅读快照——CockroachDB `8812064`、TiDB `fe7ae36`、OceanBase `ae69064`、Trino `68dae09`、Apache Doris `5202d06`。

### 源码入口索引

- **CockroachDB (`8812064`)**：[Join 探索规则 `join.opt`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/rules/join.opt#L253-L258) · [Hash/Merge Join 成本 `coster.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/xform/coster.go#L1062-L1136) · [外部 Hash Join 与 Sort-Merge 降级 `external_hash_joiner.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/colexec/colexecdisk/external_hash_joiner.go#L24-L138) · [`memo.Cost` 与分层比较函数 `cost.go`](https://github.com/cockroachdb/cockroach/blob/8812064a015d2faf99d3fc7e15880f94042954b0/pkg/sql/opt/memo/cost.go#L17-L58)
- **TiDB (`fe7ae36`)**：[`PhysicalMergeJoin` 候选生成](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L49-L124) 与 [子节点属性推导](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_merge_join.go#L425-L453) · [`PhysicalStreamAgg` 候选生成](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/operator/physicalop/physical_stream_agg.go#L39-L155) · [Cost Model V2 `plan_cost_ver2.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/core/plan_cost_ver2.go#L737-L820) · [`CostTrace` 结构 `cost_misc.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/planner/util/costusage/cost_misc.go#L49-L68) · [`HashJoinV2` 分区 Spill 与恢复 `hash_join_v2.go`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L654-L767)（恢复循环见 [`L1103-L1164`](https://github.com/pingcap/tidb/blob/fe7ae3611c83bdf64c911d496ec0509ef63cb27a/pkg/executor/join/hash_join_v2.go#L1103-L1164)）
- **OceanBase (`ae69064`)**：[Merge/Hash Join 成本模型 `ob_opt_est_cost_model.cpp`](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L285-L430) · [六类 Sort 计价分支](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L510-L576) · [Merge/Hash Group 聚合成本](https://github.com/oceanbase/oceanbase/blob/ae69064f9b3f9424997a92934978a14ad5e097ce/src/sql/optimizer/ob_opt_est_cost_model.cpp#L1125-L1187)
- **Trino (`68dae09`)**：[连接分布选择 `DetermineJoinDistributionType.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/DetermineJoinDistributionType.java#L80-L169) · [三维成本 `LocalCostEstimate.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/LocalCostEstimate.java#L33-L35) 与 [比较折叠 `CostComparator.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/cost/CostComparator.java#L54-L71) · [本地物理规划 `LocalExecutionPlanner.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/LocalExecutionPlanner.java#L2869-L2983) 与 [`SpillingJoinProcessor.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/operator/join/spilling/SpillingJoinProcessor.java#L47-L155)
- **Apache Doris (`5202d06`)**：[Nereids 排序与哈希连接计价 `CostModel.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/CostModel.java#L257-L444) · [构造折叠 `Cost.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/cost/Cost.java#L31-L52) · [分区化 Hash Join Spill 管线 `pipeline_fragment_context.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/pipeline/pipeline_fragment_context.cpp#L1761-L1810)

### 参考文献

1. Yannis E. Ioannidis et al., *Parametric Query Optimization*, [VLDB 1992](https://www.vldb.org/dblp/db/conf/vldb/IoannidisNSS92.html).
2. Goetz Graefe, *Query Evaluation Techniques for Large Databases*, [ACM Computing Surveys 1993](https://doi.org/10.1145/152610.152611).
3. Navin Kabra, David J. DeWitt, *Efficient Mid-Query Re-Optimization of Sub-Optimal Query Execution Plans*, [SIGMOD 1998](https://www.vldb.org/dblp/db/conf/sigmod/KabraD98.html).
4. Changkyu Kim et al., *Sort vs. Hash Revisited: Fast Join Implementation on Modern Multi-Core CPUs*, [PVLDB 2009](https://www.vldb.org/pvldb/vol2/vldb09-257.pdf).
5. Cagri Balkesen et al., *Multi-Core, Main-Memory Joins: Sort vs. Hash Revisited*, [PVLDB 2013](https://www.vldb.org/pvldb/vol7/p85-balkesen.pdf).
6. Maximilian Bandle, Jana Giceva, Thomas Neumann, *To Partition, or Not to Partition, That is the Join Question in a Real System*, [SIGMOD 2021](https://db.in.tum.de/~bandle/papers/bandle-partitionVsNonPartition.pdf).
7. Rong Zhu et al., *Lero: A Learning-to-Rank Query Optimizer*, [PVLDB 2023](https://www.vldb.org/pvldb/vol16/p1466-zhu.pdf).
8. Altan Birler, Thomas Neumann, Viktor Leis, *Simple, Efficient, and Robust Hash Tables for Join Processing*, [DaMoN 2024](https://db.in.tum.de/~birler/papers/hashtable.pdf).
9. Haibo Xiu et al., *PARQO: Penalty-Aware Robust Plan Selection in Query Optimization*, [PVLDB 2024](https://www.vldb.org/pvldb/vol17/p4627-xiu.pdf).
10. Kevin P. Gaffney, Jignesh M. Patel, *Is Perfect Hashing Practical for OLAP Systems?*, [CIDR 2024](https://www.vldb.org/cidrdb/2024/is-perfect-hashing-practical-for-olap-systems.html).
11. Riki Otaki et al., *Resource-Adaptive Query Execution with Paged Memory Management*, [CIDR 2025](https://www.vldb.org/cidrdb/papers/2025/p2-otaki.pdf).
12. Paul Groß et al., *Adaptive Factorization Using Linear-Chained Hash Tables*, [CIDR 2025](https://www.vldb.org/cidrdb/papers/2025/p21-gro.pdf).
13. Wentao Huang et al., *Hash Joins Meet CXL: A Fresh Look*, [CIDR 2026](https://www.vldb.org/cidrdb/2026/hash-joins-meet-cxl-a-fresh-look.html).

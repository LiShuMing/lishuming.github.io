---
title: "【优化器】从 ORCA CTE Reuse 到 StarRocks ROLLUP 下推：TPC-DS 中共享子计划的代价"
date: 2026-10-25T00:00:00+08:00
lastmod: 2026-10-25T00:00:00+08:00
slug: "orca-cte-reuse-starrocks-rollup-pushdown"
categories:
  - 数据库
tags:
  - ORCA
  - CTE
  - StarRocks
  - TPC-DS
  - Grouping Sets
  - Query Optimizer
description: "回看 ORCA/Hologres 中的 CTE 复用实践，并结合 StarRocks 的 CTE 代价模型与 ROLLUP 下推，讨论 MPP 优化器如何选择内联、共享与混合执行。"
draft: false
---

最近一周有两个看似不大的优化器改动：DataFusion 开始把物化 CTE（Materialized CTE）表示成一等逻辑计划节点；StarRocks 则在两列 `ROLLUP` 的聚合下推中，引入一个由优化器内部生成、强制复用的 CTE。它们重新提醒我：CTE 不只是 SQL 语法中的 `WITH`，还是优化器表达“多个消费者共享同一份中间结果”的基础设施。

我曾在基于 ORCA 的优化器中实现 CTE Reuse，并用 TPC-DS 查询做过验证。那段工作最容易被概括成“相同子计划只执行一次”，但实际问题远比这句话复杂：一旦把子计划变成共享生产者，谓词下推、列裁剪、数据分布和流水线调度的成本归属都会改变。复用既可能消除几次昂贵扫描，也可能把一个本来只读少量分区的分支拖成全量物化。

本文不尝试复原一组已经缺少公开日志的旧跑分，而是把当时形成的判断框架，与 StarRocks 当前可以从代码中验证的实现放在一起：**MPP 优化器应该在什么条件下把重复子计划改造成共享生产者，又需要为这个决定计算哪些代价？**

## 1. CTE Reuse 不是公共子表达式消除

先看一个简化例子：

```sql
WITH sales AS (
    SELECT item_sk, store_sk, net_paid
    FROM store_sales
    WHERE sold_date_sk BETWEEN 2451911 AND 2452275
)
SELECT item_sk, SUM(net_paid)
FROM sales
WHERE store_sk < 100
GROUP BY item_sk
UNION ALL
SELECT item_sk, SUM(net_paid)
FROM sales
WHERE store_sk >= 100
GROUP BY item_sk;
```

优化器至少有两种选择：

1. **内联（Inlining）**：复制 CTE 定义，每个分支独立做谓词下推、列裁剪、分区裁剪和 Join 重排；
2. **复用（Reuse）**：先执行一次 `sales`，由一个生产者把结果交给两个消费者。

如果只数逻辑算子，第二种方案似乎必然更便宜。但真正需要比较的是：

```text
Cost_inline
  ≈ Σ Cost(P | predicate_i, columns_i, distribution_i)

Cost_reuse
  ≈ Cost(P | union_predicate, union_columns)
    + write_or_broadcast(M)
    + Σ read(M_i)
    + exchange + spill + blocking
```

这里的 `P` 是 CTE 生产计划，`M` 是共享结果。内联方案重复执行 `P`，却可以为每个消费者保留最窄的列、最强的谓词和最合适的数据分布；复用方案只执行一次 `P`，却必须生产所有消费者所需列的并集，并处理结果缓存、广播或交换。若两个消费者的过滤条件几乎不相交，复用生产者可能读取两边数据的并集；若结果很大，复用还会引入内存占用、背压甚至落盘。

所以 CTE Reuse 更接近一个**共享物理设计选择**，而不是普通的公共子表达式消除（Common Subexpression Elimination）。它改变的不只是计算次数，还改变了计算发生在哪里、以什么分布输出、何时阻塞，以及中间结果存活多久。

## 2. ORCA 实践留下的三个判断

ORCA 使用 Memo 保存逻辑等价表达式及其物理实现。CTE 通常被拆成生产者（Producer）、消费者（Consumer）和锚点（Anchor）：生产者定义共享子计划，消费者引用其输出，锚点把两者置于同一计划作用域。这样的表示允许优化器在“保留共享”与“展开到各个消费者”之间选择，而不必在 SQL 解析阶段就决定物化。

在我当时的实现和 TPC-DS 调试中，最值得留下来的并不是某个查询快了多少，而是下面三个判断。

### 2.1 引用次数只是入口，不是代价模型

一个 CTE 被引用三次，不代表执行一次一定比执行三次便宜。真正决定收益的是生产者规模、各消费者的选择率、列集合、聚合缩减率，以及是否能共享昂贵算子。重复扫描一个经过强分区裁剪的小表，可能比物化一个宽而大的中间结果更便宜；重复执行同一组大表 Join 或高缩减率聚合，则更可能适合复用。

当前 Hologres 的公开文档也把选择拆成 `AUTO`、`INLINING` 和 `REUSE`：普通扫描、单次引用通常倾向内联；包含 `DISTINCT`、`GROUP BY` 等昂贵聚合的多次引用更可能复用。这个文档描述的是当前产品行为，不能反向证明 2020 年实现使用了完全相同的启发式，但它体现了相同的核心问题：引用次数必须与生产者成本和结果规模一起判断。

### 2.2 分布属性可能比算子成本更重要

在 MPP 系统中，CTE 的结果不是抽象的“内存表”。它带有随机分布、哈希分布、复制或单点（Singleton）等位置属性。消费者如果需要不同分布，复用可能迫使系统在共享结果之后增加多次数据交换（Exchange）；内联则可能让每个分支把 Join、聚合和分布要求一起优化。

我曾在 TPC-DS Q104 上集中排查过这一点：不同版本的计划不仅对 CTE 的处理不同，还因为基数估计和位置属性选择了不同 Join。部分路径把 CTE 结果收敛到单个执行节点，另一些路径保留分区分布；叠加相关列 NDV 估计偏差后，最终在 Hash Join 与 Merge Join 间作出了不同选择。这个案例最重要的结论不是“某种 Join 总是更快”，而是：**CTE 决策必须把下游所需分布纳入代价，不能只比较生产者自身的 CPU。**

由于当时的完整实验日志、代码提交和硬件条件没有公开保存，本文不引用旧的加速比，也不把这段经验包装成可复现实验。Q104 在这里仅作为故障模式：CTE 复用、基数估计、数据分布和 Join 选择会相互放大。

### 2.3 复用不能成为不可穿透的优化屏障

最朴素的物化会先完整执行 CTE，再让消费者过滤。这样虽然避免了重复计算，却丢掉了谓词下推和列裁剪。更合理的做法是收集所有消费者的需求：

- 生产者只输出各消费者所需列的并集；
- 如果每个消费者都有过滤条件，把这些条件做析取（OR）后下推到生产者；
- 消费者仍保留自己的原始谓词，保证语义正确；
- 对无法安全合并的 Limit、排序和分布要求保持谨慎。

这不是把所有优化都穿过 CTE，而是在共享与专用计划之间寻找一个可证明正确的交集。

## 3. StarRocks 怎样把 CTE 放进 Memo

StarRocks 当前的实现与上述结构相当接近，但代码把决策过程暴露得更清楚。SQL `WITH` 经 `RelationTransformer` 转换后，可以形成 `LogicalCTEAnchor`、`LogicalCTEProduce` 和 `LogicalCTEConsume`；消费者在早期仍可携带生产者子树，使 Memo 能继续枚举内联与复用。

| 阶段 | 内联路径 | 复用路径 |
|---|---|---|
| 逻辑变换 | `CTEAnchorToNoCTEImplementationRule` 只保留后续查询；消费者由其子树替代 | `CTEAnchorImplementationRule` 保留共享锚点 |
| 消费者实现 | `CTEConsumeInlineImplementationRule` 形成带子计划的 `PhysicalNoCTE` | `CTEConsumerReuseImplementationRule` 形成无子计划的 `PhysicalCTEConsume` |
| 物理执行 | 每个分支独立执行 | Produce 构造成 `MultiCastPlanFragment`，Consume 通过 `ExchangeNode` 接收共享输出 |

`CTEContext` 记录引用次数、生产者统计信息、消费者谓词和 Limit，也保存强制复用集合。当前源码中的几个决策点值得注意：

- CTE 只有一次引用，通常直接内联；
- `cbo_cte_reuse_ratio = 0` 倾向复用，负值关闭复用；默认值对应基于代价的选择；
- 当候选 CTE 太多时，引用少于 3 次的候选倾向内联，以控制搜索空间；
- `MATERIALIZED`，或生产者节点数达到 `cbo_cte_force_reuse_node_count`，会强制物化，主要目的是避免过大的优化搜索开销。

最后一条很容易误读。强制复用不一定得到运行时最优计划，它是在优化耗时与执行代价之间设一道保险丝。换句话说，优化器本身也是需要被预算的程序。

### 3.1 StarRocks 当前的复用代价

StarRocks 在 `CostModel.visitPhysicalCTEAnchor` 中为共享锚点计入生产结果规模：CPU 近似与 `produceSize × consumeNum × 0.5` 成正比，内存近似为 `produceSize × (1 + ratio)`；Producer 和 Consumer 节点自身返回零成本，相关代价集中在 Anchor 处理。

这个模型的好处是简单，足以让 Memo 比较复用与内联；边界也很明确：它更像一个工程代理量，而不是完整模拟。不同消费者的选择率、交换拓扑、首行延迟、并发读取、中间结果生命周期和落盘风险，并没有全部被显式建模。若统计信息未知，某些扫描的 CTE 输出行数还会被调整为零，从而倾向内联，避免在证据不足时盲目复用。

### 3.2 用消费者需求修补优化屏障

StarRocks 的 `PushLimitAndFilterToCTEProduceRule` 会收集消费者条件：当每个消费者都有谓词时，把不同谓词析取后下推到 Produce；当所有消费者只有 Limit 而没有谓词时，把最大 Limit 下推。这正是前面提到的折中：共享生产者至少不处理任何消费者都不需要的数据，同时每个消费者继续执行自己的过滤。

假设两个消费者分别需要 `store_sk < 100` 和 `store_sk >= 100 AND state = 'CA'`，生产者可以接收：

```sql
WHERE store_sk < 100
   OR (store_sk >= 100 AND state = 'CA')
```

它仍可能比两个完全独立的扫描读得更多，但不会退化为无条件扫描。对优化器而言，这也是一个提醒：评估复用成本时，应该使用合并谓词后的生产者基数，而不是原始 CTE 定义的基数。

## 4. 最新 ROLLUP 下推：CTE 作为内部共享算子

StarRocks 近期合入的 `PushDownAggregateGroupingSetsRule` 很好地说明了 CTE 基础设施为何不应局限于用户写出的 `WITH`。对于两列 `ROLLUP(a, b)`，逻辑上需要三个分组集合：

```text
()
(a)
(a, b)
```

传统执行可以先由 Repeat 扩展每一行，再分别聚合。输入为 `N` 行时，Repeat 后最多形成约 `3N` 行；如果 `(a, b)` 的基数 `G` 远小于 `N`，这会把大量重复数据送入后续聚合。

新规则先在最细粒度 `(a, b)` 上聚合一次，把结果缩减到约 `G` 行，然后创建一个内部 CTE：

1. `CTEProduce` 保存最细粒度聚合结果；
2. 一个 `CTEConsume` 继续计算 `()` 与 `(a)`；
3. 另一个 `CTEConsume` 直接提供 `(a, b)`；
4. 两条分支通过 `UNION ALL` 合并；
5. `CTEAnchor` 把这份共享结果固定在同一计划中。

对于 `AVG`，规则先分解成 `SUM` 与 `COUNT`。上层分组通过 `SUM(sum_value) / SUM(count_value)` 重建平均值，避免对局部平均值再次求平均。这一细节决定了下推是否语义正确。

这次改动没有对所有查询无条件开启。当前门槛要求输入行数至少为 `1e8`，并且输入行数至少是最细分组估计基数的 10 倍。缺少统计信息、外表分区尚未裁剪，或 CTE 消费者拿不到生产者统计信息时，规则也会放弃转换。

PR 给出的 TPC-DS SF1000 测试运行在 4 个 BE 上，数据为 Iceberg Parquet ZSTD，并已收集统计信息：

| 查询 | 修改前 | 修改后 | 主要变化 |
|---|---:|---:|---|
| Q70 | 4182 ms | 1418 ms | 约降低 66%，最大聚合输入从 16.07 亿降至 5.53 亿行 |
| Q86 | 1013 ms | 620 ms | 最大聚合输入从 4.34 亿降至 1.45 亿行 |

同一 PR 也记录了反例：对 Q05、Q77、Q80 强制下推会慢 14%–18%。这解释了为什么规则需要输入规模与缩减率双重门槛。上表是贡献者在 PR 中提供的相邻运行结果，不是本文重新控制缓存、并发和机器状态后的独立基准，因此适合证明“这个改写有现实收益且存在反例”，不适合当作跨系统性能结论。

这里最值得沉淀的设计不是两个阈值，而是：**优化器主动生成 CTE，把它当成共享子计划的内部 IR。** SQL 用户没有写 `WITH`，但 Grouping Sets 改写需要“一次生产、两路消费”，现有 CTE Anchor/Produce/Consume 恰好提供了语义与物理执行载体。

## 5. ORCA 经验与 StarRocks 实现的对应关系

| 维度 | ORCA/Hologres 实践中的关注点 | StarRocks 当前实现 | 仍需补足的模型 |
|---|---|---|---|
| 决策单位 | Producer/Consumer/Anchor 等价表达式 | Logical/Physical CTE 节点进入 Memo | 部分复用、消费者分组复用 |
| 复用条件 | 引用次数、生产者成本、结果规模 | 引用次数、比例、节点数、统计信息及规则阈值 | 用统一代价替代分散启发式 |
| 谓词下推 | 合并消费者需求，避免完整物化 | 消费者谓词 OR 后推入 Produce | 选择率相关性、分区裁剪收益 |
| 列裁剪 | 生产消费者输出列并集 | 通过列映射和需求传播处理 | 宽行序列化与缓存占用 |
| 数据分布 | Locus 影响 Join 与 Exchange | MultiCast fragment + Consumer Exchange | 各消费者不同分布要求的网络成本 |
| 聚合复用 | 高缩减率算子适合共享 | ROLLUP 先细粒度聚合，再由 CTE 分流 | 倾斜、局部聚合、两阶段聚合成本 |
| 优化耗时 | Memo 枚举也需受控 | 节点数阈值可强制复用 | 编译预算与运行成本的联合目标 |
| 失败方式 | 估计偏差改变 Join/分布/复用 | 无统计时跳过部分规则或偏向内联 | 对估计误差的鲁棒决策 |

DataFusion 最近加入 `LogicalPlan::MaterializedCte`，则从另一个方向验证了同一趋势：物化 CTE 需要成为带明确语义的计划节点，才能参与规则匹配、计划展示和后续执行。该改动目前仍有 Protobuf、Substrait 和 Unparser 未覆盖等兼容边界，但它让“共享结果”不再隐含在扫描或临时表中。

## 6. 如果重新设计一次 CTE 代价模型

若今天重新做这项工作，我会把候选方案至少分成三类，而不是只做 Inline/Reuse 二选一：

1. **全部内联**：每个消费者拥有独立、完全可下推的计划；
2. **全部复用**：一个生产者覆盖所有消费者需求；
3. **分组复用（Hybrid）**：谓词、列集合和分布相近的消费者共享，差异过大的分支单独内联。

对每个共享组，模型需要估算：

```text
Benefit
  = 被消除的扫描、Join、聚合与表达式计算

Penalty
  = 合并谓词后新增的输入
  + 合并列集合后的行宽
  + Producer 输出与生命周期占用
  + Consumer Exchange / Broadcast
  + 阻塞、背压与 Spill 风险
  + 失去分支专用 Join 顺序的机会成本
```

此外还应把优化时间单列出来。生产者展开到多个消费者会放大 Memo，复杂查询甚至可能先耗尽编译预算；强制复用能压缩搜索空间，却可能牺牲运行计划。这两个目标不能混在一个模糊的“cost”里，最好同时记录 `planning_cost` 与 `execution_cost`，再由会话超时或工作负载类型决定权重。

Hybrid 也不是理论上的奢侈品。假设四个消费者中，三个读取相同日期范围并做类似聚合，另一个只访问一天且要求不同分布。把前三个消费者归为一个共享组、最后一个内联，通常比“全复用”或“全展开”更符合实际成本。难点在于消费者聚类会扩大搜索空间，因此可以先按谓词重叠率、所需列 Jaccard 相似度和分布兼容性生成少量候选，而不是枚举所有集合划分。

## 7. 怎样用 TPC-DS 做一次可复现对比

这类优化最容易被一个总耗时数字掩盖。重新验证时，我会固定数据格式、Scale Factor、并发、统计信息版本、缓存状态和集群资源，对每个查询至少保存三类证据：计划、运行画像和优化器决策原因。

### 7.1 查询选择

- **Q70、Q86**：观察 `ROLLUP`/Grouping Sets 下推，验证最细粒度聚合能否显著缩减 Repeat 输入；
- **Q104**：观察多消费者 CTE 与后续 Join，重点检查基数、NDV、分布和 Join 方法；
- 选取一个各消费者谓词高度重叠的 CTE，验证复用收益；
- 选取一个消费者谓词互斥且选择率很高的 CTE，验证内联收益；
- 选取一个宽行、大结果的 CTE，触发内存压力与 Spill 边界。

### 7.2 不只记录 Query Latency

| 层次 | 建议记录的指标 |
|---|---|
| 优化器 | 计划时间、Memo group/expression 数、选择 Inline/Reuse 的原因、关键基数与 NDV |
| Produce | 输入/输出行数、输出字节、聚合缩减率、峰值内存、Spill 字节、首批输出时间 |
| Consume | 每个消费者读取行数、过滤后行数、所需列、等待时间、重复读取次数 |
| 网络 | Exchange 类型、发送/接收字节、Broadcast 放大倍数、分区倾斜 |
| 下游 | Join 方法、Build/Probe 行数、局部/全局聚合输入、分区裁剪数量 |
| 端到端 | 冷/热缓存耗时、P50/P95、多次运行方差、并发下吞吐 |

测试至少包含 `AUTO`、强制 `INLINING`、强制 `REUSE` 三组，并核对结果集。对于 StarRocks 的 Grouping Sets 规则，还应分别在阈值两侧构造数据，验证统计误差是否会让规则在接近边界时抖动。若只能观察到总耗时，无法区分收益来自共享计算、缓存命中、Join 变化还是偶然的集群负载。

## 8. 结论：把 CTE 当成数据流分叉点

从 ORCA 到 StarRocks，CTE Reuse 的核心问题没有改变：共享能消除重复计算，但共享本身也有价格。引用次数只是最粗的信号；生产者缩减率、消费者需求重叠、数据分布、网络交换、内存生命周期和优化搜索空间，才共同决定结果。

StarRocks 最近的 ROLLUP 下推给出了一个很好的工程样本：先用统计信息确认“预聚合足够划算”，再生成内部 CTE 共享最细粒度结果，并用真实反例约束启发式。它也说明 CTE 最有价值的定位不是“是否物化 SQL `WITH`”，而是**优化器用来表达数据流分叉与共享的通用原语**。

当前实现仍有清晰边界：StarRocks 的 Anchor 成本是代理模型，PR 跑分不是独立基准；Hologres 文档反映当前策略，不能替代历史实现记录；我过去的 Q104 调试缺少完整公开日志，因此只保留机制层结论。下一步若要把这段经验继续沉淀，最值得做的不是再增加一个固定阈值，而是建立一套可解释的 CTE 决策画像：对每个候选同时展示 Inline、Reuse 与 Hybrid 的输入规模、共享收益、Exchange、内存和计划时间，让优化器的选择可以被验证，也可以被推翻。

## 参考资料

- [Optimization of Common Table Expressions in MPP Database Systems](https://www.vldb.org/pvldb/vol8/p1704-elhelw.pdf)
- [Orca: A Modular Query Optimizer Architecture for Big Data](https://dl.acm.org/doi/pdf/10.1145/2588555.2595637)
- [Hologres：CTE 物化策略](https://www.alibabacloud.com/help/en/hologres/user-guide/cte-materialization-strategy)
- [StarRocks PR #81468：Push down aggregate for two-key ROLLUP](https://github.com/StarRocks/starrocks/pull/81468)
- [StarRocks：CTEContext](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEContext.java)
- [StarRocks：CTEUtils](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEUtils.java)
- [StarRocks：RelationTransformer 中的 CTE 转换](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/transformer/RelationTransformer.java)
- [StarRocks：PushLimitAndFilterToCTEProduceRule](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushLimitAndFilterToCTEProduceRule.java)
- [StarRocks：PushDownAggregateGroupingSetsRule](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushDownAggregateGroupingSetsRule.java)
- [StarRocks：CostModel](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java)
- [StarRocks：PlanFragmentBuilder](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/plan/PlanFragmentBuilder.java)
- [DataFusion PR #26269：Represent materialized CTE as first-class logical plan node](https://github.com/apache/datafusion/pull/26269)

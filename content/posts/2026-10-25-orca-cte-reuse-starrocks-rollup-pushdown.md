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
description: "为什么把 WITH 子句只算一次有时反而更慢？结合 ORCA/Hologres 的 TPC-DS 实战经验与 StarRocks 的 CTE 竞价及两列 ROLLUP 下推源码，拆解分布式引擎中内联、共享与混合分组复用的真实成本账单。"
draft: false
toc: true
math: true
---

写复杂分析 SQL 时，几乎每位工程师都有过这样的直觉：把重复出现的子查询抽进 `WITH` 子句（公共表表达式，Common Table Expression，简称 CTE），让数据库“只算一次、到处复用”，肯定比复制粘贴算两遍更快。

然而只要把这种直觉搬进真实的分布式 MPP 数据库，就会频繁撞见诡异的反例——明明把重复代码合进了同一个 `WITH` 并强制物化，原本几百毫秒跑完的查询反而卡成了十几秒，甚至直接触发内存超限落盘（Spill）。

最近开源社区恰好有两个耐人寻味的动作撞在了一起：一边是 Apache DataFusion 合入 [PR #26269](https://github.com/apache/datafusion/pull/26269)，把物化 CTE（`LogicalPlan::MaterializedCte`）正式提升为一等逻辑计划节点；另一边是 StarRocks 合入 [PR #81468](https://github.com/StarRocks/starrocks/pull/81468)，在处理两列 `ROLLUP(a, b)` 聚合下推时，在用户根本没写 `WITH` 的情况下，由优化器内部主动构造出一个强制复用的 CTE 来分流中间数据。

这两件事把一个老问题重新推到了聚光灯下：**在分布式查询优化器眼里，CTE 根本不只是 SQL 语法糖，而是表达“数据流分叉与中间结果共享”的核心物理原语。** 早年我在基于 ORCA 架构的优化器里实现 CTE Reuse 并死磕 TPC-DS 复杂查询时，踩过不少共享子计划的深坑。今天我们就把当年沉淀下来的判断框架，与 StarRocks 当前源码里可逐行验证的机制摆在一起，算清楚 MPP 优化器到底该在什么条件下把重复子计划合并成共享生产者，又必须为这个决定支付哪些隐性账单。

---

## 一、为什么 CTE 复用不是“公共子表达式消除”？

在传统编译器里，遇到 `x = a + b` 被用了两次，做一次公共子表达式消除（Common Subexpression Elimination，CSE）把结果存进寄存器几乎稳赚不赔。但在关系型数据库里，子计划不是无状态的标量运算，它紧咬着底层存储的分区裁剪、列式投影以及跨节点的网络分布。

看一条最简单的双分支查询：

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

拿到这棵逻辑树，优化器面前摆着两条截然不同的路：

1. **内联展开（Inlining）**：把 `sales` 的定义原样复制两份塞进上下两个分支，每个分支各自独立走后续的谓词下推、列裁剪、连接重排与分布规划；
2. **共享复用（Reuse）**：只启动一个 `sales` 生产者（Producer）算一遍中间结果，再通过内存缓冲或多播管道喂给两个消费者（Consumer）。

如果只数计划树上有几个 `Scan` 算子，复用方案少扫了一次表，看似赢定了。但如果我们把两侧在执行引擎里真正发生的物理开销列成等式，画风立刻就变了：

$$\text{Cost}_{\text{inline}} \approx \sum_{i=1}^{k} \text{Cost}\!\left(P \;\middle|\; \sigma_i, \pi_i, \text{Dist}_i\right)$$

$$\text{Cost}_{\text{reuse}} \approx \text{Cost}\!\left(P \;\middle|\; \bigvee_{i=1}^{k} \sigma_i,\; \bigcup_{i=1}^{k} \pi_i\right) + \text{Cost}_{\text{multicast/buffer}}(M) + \sum_{i=1}^{k} \left(\text{Cost}_{\text{read}}(M_i) + \text{Cost}_{\text{exchange}}(\text{Dist}_i)\right)$$

盯着这两个式子里的条件参数看，差距全在细节里：

- **裁剪与选择率倒挂**：如果第一个消费者其实只查某一天、某一个门店的热点小分区（比如额外带了高选择率条件），内联展开后可以直接命中底层存储的分区和 Min/Max 索引，只读几兆字节；而一旦强行合并成一个共享生产者，生产者就必须读取**所有消费者所需列的并集（`∪ π_i`）与过滤条件的析取并集（`∨ σ_i`）**，把原本轻巧的局部点查拖成笨重的宽表大范围扫描。
- **连接顺序与分布绑架**：内联时，分支一可以按 `item_sk` 哈希分布直接对接聚合，分支二如果要去连另一张大表可以改按连接键分布；一旦共享同一份中间结果 `M`，所有下游消费者拿到的都是同一种物理分布，不合胃口的分支只能自己掏钱补插网络重洗牌（`Exchange`）。
- **流水线阻塞与落盘风险**：流式内联计划边扫边聚合，内存里只留几个 Batch；而多路共享一旦遇到某个消费者消费速度慢（比如卡在下游 Hash Join 的 Build 端），多播缓冲区就会积压产生反压（Backpressure），结果集一大甚至被迫溢写磁盘。

因此，CTE 复用从来不是简单的“消除重复算子”，而是一场**用“拓宽单次扫描范围 + 破坏各分支专属优化 + 中间缓冲与网络重分布开销”去换取“免除高昂子计划重复计算”的物理交易**。

---

## 二、ORCA 与 TPC-DS 实战留下的三条铁律

在经典的 [ORCA](https://dl.acm.org/doi/pdf/10.1145/2588555.2595637)（Greenplum / Pivotal 开源的模块化 Cascades 优化器）论文与实现中，为了让“内联还是复用”真正交由代价模型裁决，而不是在 SQL 解析完那一刻就拍脑袋写死，CTE 被拆解为三个配套算子装进 Memo：**`Anchor`（锚点，界定共享结果的有效作用域）**、**`Producer`（生产者，定义共享子计划）** 和 **`Consumer`（消费者，按 ID 引用生产者输出）**。这样逻辑变换规则就可以在同一个等价类里同时生成“脱掉锚点全部内联”和“保留锚点一次生产多处消费”两种候选，交给底层的物理属性与代价引擎同台竞技：

![GPORCA 公共表表达式（CTE）内联展开 vs 物化 Spool 代价决策流](/images/diagrams/opt-orca-cte-decision.svg)

*图 1：GPORCA 中 CTE 的双轨枚举与代价裁决——同一 `Group` 内并行展开全内联路线（各分支独立下推裁剪与对齐分布）与物化 `Spool` 共享路线（联合下推列并集与析取谓词、阻塞缓冲防流水线死锁、按需补插 `Exchange`），最终由 CBO 统一比价拍板。*

回看当年基于 ORCA 实现 CTE Reuse 并在 TPC-DS 全集上打磨计划的经历，真正经得起时间检验的不是某条 SQL 的局部调参，而是下面三条核心铁律。

### 2.1 引用次数只是入场门票，缩减率与算子昂贵度才是胜负手

一个 `WITH` 被引用了三次，绝不代表算一次就比算三次省钱。真正决定盈亏平衡点的是**生产者子计划到底干了多少重活，以及吐出的结果缩减到了多小**。

设想生产者只是一个带过滤条件的单表扫描：哪怕扫三遍，靠着各自的分区裁剪和操作系统页缓存，开销也微乎其微；可如果把几千万行宽表物化进多播通道再分发三次，序列化与内存拷贝当场倒贴。反过来，如果生产者内部已经做完了五张大表的复杂 Hash Join，紧接着又做了一层高缩减率的 `GROUP BY` 或 `DISTINCT`，把十亿行原始数据压成了十万行汇总结果——这时候复用这份高含金量的浓缩产物，就能一次性砍掉后面好几轮百亿行级别的扫描与连接！这也是为什么后来 [Hologres 等引擎在公开文档中](https://www.alibabacloud.com/help/en/hologres/user-guide/cte-materialization-strategy) 把策略拆成 `AUTO`、`INLINING` 与 `REUSE` 时，明确将普通单表扫描倾向内联，而把含 `DISTINCT`、`GROUP BY` 等重度算子的多次引用列为复用主力。

### 2.2 在 MPP 引擎里，数据分布属性往往比算子自身 CPU 更致命

单机数据库看 CTE 结果，看到的是一张内存临时表；分布式 MPP 优化器看 CTE 结果，首先看到的必须是它的**物理位置属性（Locus / Distribution）**——它究竟是收敛在单个协调节点（Singleton），还是按某几列哈希分布在全集群（Hash Partitioned），抑或是全节点复制（Replicated）？

当年在排查 TPC-DS 扩展集里的硬骨头 **Q104** 时，这一点展现得淋漓尽致。面对复杂的多消费者 CTE 嵌套多表连接，一旦某条候选路径把共享 CTE 的输出收敛到了单节点（或者赋予了与下游连接键不匹配的哈希分布），叠加多列相关性导致的基数（NDV）估算偏差，下游连接算子的天平就会剧烈摇摆——原本该走分布式并行 Hash Join 的地方可能被迫插入全量重洗牌，或者误判成带昂贵排序补偿的 Merge Join。那次排障给人最深的教训就是：**评估 CTE 复用时，如果只算生产者省下了多少 CPU，而不把每个消费者为了适配下游算子额外付出的 `Exchange` 网络费算进同一笔总账，选出来的“局部最省计划”在集群上跑起来往往是灾难。**

### 2.3 共享生产者绝不能变成阻断下推的“黑盒屏障”

最粗糙的 CTE 物化实现会把生产者当成神圣不可侵犯的黑盒：老老实实把 `WITH` 里写的全量数据一字不落地算完，再让外层各个消费者自己去过滤和挑列。这种做法等于亲手废掉了优化器最值钱的谓词下推与列裁剪武功。

要让共享路径具备竞争力，优化器必须在拍板之前做一次**跨消费者的需求求交与求并**：

- **列裁剪求并集**：统计所有消费者实际用到的输出列并集，把谁都没碰过的冗余宽列直接从生产者里裁掉；
- **过滤条件求析取（`OR`）下推**：只要每一个消费者都在同一批列上挂了过滤条件，就把各分支的谓词用 `OR` 拼起来推入生产者（同时每个消费者头顶必须原样保留自己的精确过滤条件以守住语义正确性！）；
- **谨慎对待 `LIMIT` 与排序**：只有当所有消费者都没有额外过滤且都带 `LIMIT` 时，才能安全地把最大的那个 `LIMIT` 推下去。

---

## 三、StarRocks 如何把 CTE 装进 Cascades Memo 竞价

把视线拉回现在的开源工业界，翻开 StarRocks FE 源码就会发现，它与 ORCA 的设计哲学一脉相承，而且把工程防线画得更加直白。

当一条带 `WITH` 的 SQL 经过 [`RelationTransformer.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/transformer/RelationTransformer.java) 转换后，同样会生成 `LogicalCTEAnchor`、`LogicalCTEProduce` 与 `LogicalCTEConsume` 三件套；巧妙的是，早期逻辑阶段的 `LogicalCTEConsume` 底下依然挂着生产者的完整子树副本，方便后续规则在 Memo 里自如分叉出两条物理实现路线：

| 规划阶段 | 内联展开路线（Inline） | 共享复用路线（Reuse） |
| --- | --- | --- |
| **锚点转换规则** | `CTEAnchorToNoCTEImplementationRule` 剥离锚点，直接透传主查询 | `CTEAnchorImplementationRule` 保留 `PhysicalCTEAnchor` 统筹作用域 |
| **消费端实现规则** | `CTEConsumeInlineImplementationRule` 转成携带完整子计划的 `PhysicalNoCTE` | `CTEConsumerReuseImplementationRule` 转成轻量的无子计划节点 `PhysicalCTEConsume` |
| **分布式执行落地** | 各分支完全独立生成 Fragment，各自下推裁剪 | [`PlanFragmentBuilder.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/plan/PlanFragmentBuilder.java) 将生产端封装为 `MultiCastPlanFragment`，各消费端通过 `ExchangeNode` 接收多播流 |

在进 Memo 搜索前后，[`CTEContext.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEContext.java) 与 [`CTEUtils.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEUtils.java) 负责替优化器把控大局：

- 单次引用的 CTE 直接内联，不浪费半点搜索空间；
- 会话变量 `cbo_cte_reuse_ratio` 充当方向盘（负值一刀切全内联，`0` 倾向复用，默认正值走正常的 CBO 代价竞争）；
- 当一条变态 SQL 里定义了过多候选 CTE 时，引用不足 3 次的轻量 CTE 会优先内联，防止 `2^K` 级别的锚点组合把 Memo 撑爆；
- 如果用户显式写了 `WITH ... AS MATERIALIZED`，或者生产者子树算子数量突破了阈值 `cbo_cte_force_reuse_node_count`，系统会**跳过比价直接强制复用**！

千万别把最后这条规则误读成“节点多的子查询复用跑得一定更快”——它其实是一根保护优化器自身的**编译期保险丝**！试想一个包含 80 个算子的巨型 CTE 被引用了 6 次，如果允许它内联展开，整棵查询树瞬间膨胀到近 500 个算子，还没等执行引擎干活，FE 优化器自己的连接重排和内存预算就先超时熔断了。

当候选真正进入 Memo 比价时，[`CostModel.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java) 的 `visitPhysicalCTEAnchor` 用一套简明扼要的代理公式给“多播共享税”明码标价：`Produce` 和 `Consume` 节点本身记零成本，整笔缓冲与分发开销统一记在 `PhysicalCTEAnchor` 头上——CPU 成本近似正比于 `produceSize × consumeNum × 0.5`，内存成本记为 `produceSize × (1 + ratio)`（若底层表缺失统计信息，特定扫描行数的估计会被保守归零以偏向安全的内联路径）。

与此同时，[`PushLimitAndFilterToCTEProduceRule.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushLimitAndFilterToCTEProduceRule.java) 把我们在 ORCA 里强调的“穿透屏障”落到了实处：当两个消费者分别要求 `store_sk < 100` 和 `store_sk >= 100 AND state = 'CA'` 时，该规则会自动提取并拼出析取条件：

```sql
WHERE store_sk < 100
   OR (store_sk >= 100 AND state = 'CA')
```

将其一把推入 `LogicalCTEProduce`（若所有消费者均无过滤且只带 `LIMIT`，则把最大的 `LIMIT` 推入生产端）。这样一来，共享生产者至少绝不会去陪读任何消费者都不关心的废数据，而且后续 CBO 在评估 `produceSize` 时，用的也是经过这层联合谓词裁剪后的真实瘦身基数。

---

## 四、超越 SQL 语法：两列 `ROLLUP` 下推为何要在内部无中生有一个 CTE？

如果说上面的机制还停留在“优化用户手写的 `WITH`”，那么 StarRocks 新近合入的 [`PushDownAggregateGroupingSetsRule.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushDownAggregateGroupingSetsRule.java)（[PR #81468](https://github.com/StarRocks/starrocks/pull/81468)）则展示了一个更漂亮的跃迁：**当某种代数改写天然需要“一次计算、分流消费”时，优化器直接把内部 CTE 当成通用中间表示（IR）无中生有地造出来！**

让我们从第一性原理算一算多维分析里最常见的两列上卷聚合 `GROUP BY ROLLUP(a, b)`。在语义上，它要求一次性算出三个粒度的汇总结果：`()`（总计）、`(a)`（按 `a` 小计）以及 `(a, b)`（最细粒度明细聚合）。

传统的标准执行套路是挂一个 **`RepeatNode`**：来一条原始输入行，先在内存里把它翻倍复制成 3 行（分别打上不同的分组掩码 `grouping_id`），再一股脑塞进后面的哈希聚合算子。假设底层扫描或连接吐出了 `N = 5 亿` 行原始数据，经过 `RepeatNode` 一吹气，涌向聚合算子的输入瞬间暴涨到 **`3N = 15 亿` 行**！

问题出在哪儿？在绝大多数真实报表里，最细粒度 `(a, b)` 的不同组合数（基数 `G`）远远小于原始行数 `N`——比如 5 亿条销售流水，按 `(省份, 门店)` 聚完可能只剩区区 `G = 5 万` 行！既然 `()` 和 `(a)` 完全可以在 `(a, b)` 的预聚合结果之上二次汇总出来，先复制三遍去硬扛 15 亿行哈希探测，纯属白烧 CPU 和网络带宽！

看破这一点后，`PushDownAggregateGroupingSetsRule` 做了一次极其优雅的手术：先在底层表或预聚合 `ROLLUP` 索引吐出的原始输入上按最细粒度 `(a, b)` 做一次聚合，把 `N` 行瞬间压扁到 `G` 行，紧接着**在计划树内部凭空捏造一组强制复用的多播 CTE 流水线**：

![StarRocks 预聚合改写与强制多播 CTE 执行流水线](/images/diagrams/opt-starrocks-rollup-cte-pipeline.svg)

*图 2：StarRocks 细粒度预聚合改写与强制多播 CTE 管道——输入满足缩减门槛后先压缩为 `G` 行（并将 `AVG` 拆成 `SUM` 与 `COUNT`），再由 `MultiCastPlanFragment` 分流给零计算直出 `(a, b)` 的分支 1 与执行 `Repeat` 二次补偿上卷的分支 2。*

注意图里一个关乎数据正确性的魔鬼细节：如果原查询算的是平均值 `AVG(x)`，二次汇总绝不能对 `(a, b)` 算出来的局部平均值再求平均（否则各组样本量不等时结果全错）！因此规则在底层 `CTEProduce` 里先把 `AVG` 拆成 `SUM(x)` 和 `COUNT(x)` 两列中间状态，到了分支 2 的上层聚合再通过 `SUM(sum_val) / SUM(count_val)` 精确还原。

那么，是不是所有两列 `ROLLUP` 这么改都稳赚？绝对不是！设想极端反例：如果 `(a, b)` 接近唯一主键（即基数 `G ≈ N`，几乎压不掉行数），硬加一层预聚合加 `MultiCastPlanFragment` 多播分发，不仅一行输入都没省下，反而白白多建了一次大哈希表、多走了一遭跨 Fragment 网络管道！

为此，该规则设下了严苛的统计准入门槛：**要求输入行数至少达到 `1e8`（一亿行），且输入行数至少是最细粒度分组估计基数 `G` 的 10 倍（缩减率 `≥ 10×`）**；一旦基表缺统计信息、外表分区尚未裁剪，或者消费端拿不到生产端统计，规则立即安静退出。在 PR 附带的 4 节点 BE、TPC-DS SF1000（Iceberg Parquet ZSTD，开启统计信息）实测对比中，高缩减率查询收益显著：

| TPC-DS 查询 | 改写前耗时 | 改写后耗时 | 核心执行画像变化 |
| --- | ---: | ---: | --- |
| **Q70** | `4182 ms` | `1418 ms` | 耗时下降约 66%，最大聚合算子输入从 `16.07 亿` 行锐减至 `5.53 亿` 行 |
| **Q86** | `1013 ms` | `620 ms` | 最大聚合算子输入从 `4.34 亿` 行降至 `1.45 亿` 行 |

与此同时，作者在 PR 里也诚实记录了强行绕过阈值开关后的反例：在缩减率不够高的 **Q05、Q77、Q80** 上，硬推内部 CTE 反而让查询变慢了 **14%～18%**——这恰好用实测数据印证了我们在第一节推导的成本定律：**多播缓冲与二次聚合的固定税费客观存在，只有当预聚合砍掉的行数红利足够丰厚时，共享子计划这笔买卖才有利可图。**

---

## 五、从二选一走向分组混合复用：重塑 CTE 代价模型

把早年的 ORCA / Hologres 实战经验、DataFusion 的 `MaterializedCte` 节点演进（目前仍在逐步补齐 Protobuf、Substrait 序列化与 SQL Unparser 等周边生态）以及 StarRocks 的最新实现放在同一张坐标图里，能清晰看到工业界在共享子计划优化上的演进脉络与未竟之地：

| 核心维度 | ORCA / Hologres 实践焦点 | StarRocks 当前源码落地 | 下一代代价模型仍待补齐的拼图 |
| --- | --- | --- | --- |
| **中间表示载体** | `Anchor / Producer / Consumer` 入 Memo | 逻辑/物理 CTE 三件套 + 内部改写主动生成 | 打破全有或全无，支持消费者子集**部分复用** |
| **准入与熔断控制** | 引用次数结合昂贵算子特征识别 | `cbo_cte_reuse_ratio` + 算子数超限强制复用防爆栈 | 将编译期搜索预算与运行期代价统一建模 |
| **穿透优化屏障** | 收集消费端需求反哺生产端 | 消费端谓词 `OR` 析取下推 + 最大无过滤 `LIMIT` 下推 | 跨列相关性选择率估算与精确分区裁剪联动 |
| **分布式物理税单** | Locus 分布转换触发下游连接震荡（如 Q104） | `PhysicalCTEAnchor` 线性代理公式 + `MultiCastPlanFragment` | 显式计入各消费端异构分布需求引发的 `Exchange` 成本 |
| **高缩减算子协同** | 重度聚合与多表连接优先共享 | `ROLLUP` 先压细粒度基数再借内部 CTE 分流 | 数据倾斜感知与多阶段局部/全局聚合联合寻优 |

如果今天让我们在一套新引擎里重新设计 CTE 优化器，最值得突破的一步，就是打破“要么全部内联（All-Inline）、要么全部共享（All-Reuse）”的非黑即白二选一，引入第三种形态——**分组混合复用（Hybrid Grouped Reuse）**。

试想一条复杂报表里的 `WITH` 被引用了 4 次：其中消费者 1、2、3 都在查“过去一整年的数据”并按相似维度汇总，唯独消费者 4 加了一个极窄的过滤条件“只查昨天某一个异常订单”且需要截然不同的连接分布。如果全复用，消费者 4 被迫陪等全年大表物化；如果全内联，消费者 1、2、3 又要把全年大表冤枉地扫三遍！最聪明的解法显然是**让消费者 1、2、3 抱团共享一个 Producer，同时把特立独行的消费者 4 单独内联拆出去**。

为了不让集合划分把搜索空间炸成贝尔数（Bell Number），工程上可以先按各消费者的谓词重叠度、所需列集合的 Jaccard 相似度以及下游期望分布做一次轻量聚类，只挑出两三个最有希望的分组方案送进 Memo，再按下面这张明细账单逐项比价：

$$\text{NetGain} = \underbrace{\sum \text{Saved}(\text{Scan} + \text{Join} + \text{Agg})}_{\text{消除重复计算的收益}} - \underbrace{\left(\Delta\text{WideScan} + \text{MulticastBuffer} + \sum \text{ConsumerExchange} + \text{SpillRisk} + \text{LostJoinOrder}\right)}_{\text{合并需求、缓冲分发与丧失专属计划的代价}}$$

同时，把优化器自己的规划耗时预算（`planning_cost`）和查询运行耗时（`execution_cost`）分开记账：当触发算子数保险丝强制物化时，在 `EXPLAIN` 里坦诚告诉用户“此处复用是为了防止编译超时”，而不是伪装成代价最优。

---

## 六、面向 TPC-DS 的可观测验证方法

由于共享子计划横跨了扫描裁剪、内存多播、网络洗牌和下游连接选型，做性能验证时最忌讳只贴一张端到端总耗时柱状图——哪怕总时间快了 30%，你也分不清到底是少扫了磁盘、碰巧蒙对了下游 Hash Join 方向，还是单纯蹭到了操作系统的文件缓存。

一套经得起推敲的 TPC-DS 对照实验，应当固定数据规模（如 SF1000）、存储编码、集群并发与统计信息快照，针对性挑选五类靶子查询（测 `ROLLUP` 缩减率的 **Q70 / Q86**、测多消费者分布与连接联动的 **Q104**、谓词高度重叠的多引用 CTE、谓词完全互斥的高选择率 CTE，以及触发内存水位线的超宽行大结果集 CTE），在 `AUTO`、强制 `INLINING`、强制 `REUSE`（以及规则阈值临界点两侧的数据集）下完整采集六层执行画像：

| 观测层级 | 必须核对的核心指标 |
| --- | --- |
| **优化器决策层** | 规划耗时、Memo 等价组与表达式总数、最终拍板 Inline / Reuse 的具体规则或成本差值、关键节点估算基数与真实行数偏差 |
| **Producer 生产端** | 联合谓词下推后的实际扫描分区数与行数、输出字节数、预聚合缩减率、多播缓冲区峰值内存、Spill 落盘字节与首行吐出延迟 |
| **Consumer 消费端** | 各分支实际拉取行数、二次过滤留存率、最慢分支引发的多播等待（Blocked Time）时长 |
| **网络传输层** | 消费端下游补插的 `Exchange` 类型、跨节点发送字节数、Broadcast 放大倍数与目标分桶倾斜度 |
| **下游算子联动** | 下游 Join 算子类型及 Build / Probe 左右方向是否因复用发生翻转、局部与全局聚合阶段变化 |
| **端到端稳定性** | 冷热缓存耗时对比、P50 / P95 分位延迟、并发压测下的集群内存水位与吞吐变化 |

归根结底，从 ORCA 到 DataFusion 再到 StarRocks 的 `ROLLUP` 下推，优化器演进的方向越来越清晰：不要再把 CTE 仅仅当成用户手写的语法块，而要把它打磨成内核手里收放自如的**数据流分叉算子**——算得清缩减红利、穿得透下推屏障、付得起分布路费，共享子计划才能真正从“凭运气调参”变成“凭账本说话”。

## 参考资料

- Amr El-Helw et al., [*Optimization of Common Table Expressions in MPP Database Systems*](https://www.vldb.org/pvldb/vol8/p1704-elhelw.pdf), PVLDB 2015
- Mohamed A. Soliman et al., [*Orca: A Modular Query Optimizer Architecture for Big Data*](https://dl.acm.org/doi/pdf/10.1145/2588555.2595637), SIGMOD 2014
- [阿里云 Hologres 文档：CTE 物化策略（AUTO / INLINING / REUSE）](https://www.alibabacloud.com/help/en/hologres/user-guide/cte-materialization-strategy)
- [StarRocks PR #81468：Push down aggregate for two-key ROLLUP](https://github.com/StarRocks/starrocks/pull/81468)
- [DataFusion PR #26269：Represent materialized CTE as first-class logical plan node](https://github.com/apache/datafusion/pull/26269)
- StarRocks 核心源码入口：[`RelationTransformer.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/transformer/RelationTransformer.java)、[`CTEContext.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEContext.java)、[`CTEUtils.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/CTEUtils.java)、[`PushLimitAndFilterToCTEProduceRule.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushLimitAndFilterToCTEProduceRule.java)、[`PushDownAggregateGroupingSetsRule.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation/PushDownAggregateGroupingSetsRule.java)、[`CostModel.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java)、[`PlanFragmentBuilder.java`](https://github.com/StarRocks/starrocks/blob/2aa0822f6c5b2f8e93fe7115c249b8841acb7c96/fe/fe-core/src/main/java/com/starrocks/sql/plan/PlanFragmentBuilder.java)

---

> **源码与数据基线说明**：文中 StarRocks 源码路径均固定于上游提交 `2aa0822`（2026-10）；第 四 节引用的 TPC-DS SF1000（Q70、Q86 及反例 Q05/Q77/Q80）耗时与行数对比取自 [StarRocks PR #81468](https://github.com/StarRocks/starrocks/pull/81468) 在 4 节点 BE、Iceberg Parquet ZSTD 环境下的实测记录；第 二 节关于 ORCA 与 TPC-DS Q104 的分析整理自作者过往的内核研发排障实践，聚焦计划形态与分布代价的机制推演。

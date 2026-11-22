---
title: "物化视图深水区：SPJG 透明改写、AggState 组合代数、湖仓分区治理与自动推荐闭环"
date: 2026-11-22T18:45:00+08:00
lastmod: 2026-11-22T18:45:00+08:00
slug: "mv-transparent-rewrite-aggstate-and-recommendation"
categories:
  - 数据库
  - 大数据
tags:
  - 物化视图
  - 透明改写
  - SPJG
  - AggState
  - Iceberg
  - TTL
  - 物化视图推荐
  - StarRocks
  - Apache Calcite
  - Apache Doris
  - ClickHouse
  - BigQuery
  - Snowflake
  - Redshift
  - Databricks
  - MaxCompute
description: "围绕宽表加速与 Cube 替换的真实工程张力，结合 StarRocks、Apache Calcite、Apache Doris、ClickHouse 源码及 BigQuery、Redshift、Snowflake、Databricks、MaxCompute 的云数仓实践，系统拆解 SPJG 透明改写与四类补偿机制、AggState 聚合状态组合代数、多列分区/Iceberg 变换与表达式 TTL 治理，以及基于 What-If 核验与云成本 ROI 模型的物化视图自动推荐闭环。"
draft: false
toc: true
math: true
---

设想一家电商或广告分析平台，底层湖仓（Iceberg + OLAP 内表）存着按 `(event_date, region)` 双列分区的百亿行订单事实表 `orders`，关联着客户维表 `customers` 与商品维表 `products`。每天有上千张 BI 看板和两万条定时报表 SQL 反复查询它的不同维度组合：有的按 `region` 算近 90 天的 `AVG(amount)` 与 `PERCENTILE_APPROX(latency, 0.99)`，有的只查 `orders ⋈ customers` 不碰商品表，还有的要看截至“今天这一分钟”的实时大盘——而今天这个分区的数据每秒都在写入。

如果沿用早期数仓或 Apache Kylin / Druid Cube 的老办法，团队很快会在四个地方同时卡死：

1. **改 SQL 的维护地狱**：每建一张汇总宽表，就要逼业务方改写几百个看板的 SQL 指向新表名；一旦汇总粒度微调，全线看板跟着返工。引擎必须做到**透明改写（Transparent Rewrite）**——用户继续查原始基表，优化器悄悄把查询改写到最合适的物化视图上，且当今天分区还没刷完时自动拼回基表最新数据。
2. **不可加聚合的死胡同**：物化视图如果直接存 `AVG(amount)` 或 `PERCENTILE_APPROX(latency, 0.99)` 算出来的终态标量，那么按天建好的视图既没法二次上卷（Roll-up）成月报，也没法跟今天刚扫出来的基表增量做 `UNION ALL` 合并。
3. **湖仓多列分区与生命周期的失控**：现实表几乎都不是简单的单列 `dt` 分区，而是 `(dt, region)` 多列分区或 Iceberg 的 `day(ts)` / `bucket(user_id, 16)` 隐藏变换；传统的“保留最近 30 个分区（`partition_ttl_number = 30`）”在每天 50 个 region 的多列分区下连一天数据都留不住，而在嵌套物化视图（`MV1 → MV2`）里，上游过期清理稍有不慎就会把下游保留一年的历史年报整片删光。
4. **“建了没人用、刷得比查还贵”的账单失控**：集群里每天跑几十万条 SQL，靠 DBA 肉眼挑视图注定跟不上业务变化；可如果推荐系统只看查询扫描量不看基表写入频率，或者离线推荐出来的视图上线后优化器根本改写不中，只会造出一堆每天白烧算力刷新的“僵尸视图”。

这四个问题环环相扣，正好构成了现代云数仓与 Lakehouse 物化视图子系统的四根支柱：**SPJG 透明改写**、**`AggState` 聚合状态组合**、**湖仓多列分区与表达式 TTL 治理**，以及**成本驱动的自动推荐闭环**。本文以 StarRocks、Apache Calcite、Apache Doris 和 ClickHouse 的源码实现为基准，横向对照 BigQuery、Amazon Redshift、Snowflake、Databricks 和 MaxCompute 的架构取舍，把这四块深水区彻底讲透。

---

## 一、SPJG 透明改写内核：从结构匹配到四类补偿武器

透明改写的本质不是字符串替换，而是一道**关系代数等价性证明题**：给定用户查询树 $Q$ 和物化视图定义树 $V$，优化器能否构造出一棵引用物理表 $V_{\text{scan}}$ 的新查询树 $Q'$，使得在当前数据快照下 $Q' \equiv Q$ 恒成立，且 $Q'$ 的物理执行代价低于 $Q$？

2001 年 Jonathan Goldstein 与 Per-Åke Larson 在 SIGMOD 提出的经典 SPJG（Select–Project–Join–GroupBy）改写框架奠定了整个工业界的基石；后来 Apache Calcite 将其工程化为 `MaterializedViewRule`，StarRocks 的 `MaterializedViewRewriter` 与 Apache Doris Nereids 的 MTMV 改写器又在其上补齐了面向多表宽表、湖仓分区补偿与高并发 CBO 剪枝的大量实战能力。

![SPJG 物化视图透明改写全链路：结构匹配、三类补偿机制与 Union 分区新鲜度兜底](/images/diagrams/mv-spjg-rewrite-and-compensation.svg)

### 1.1 六步改写流水线：为什么必须放进 CBO 竞价而不是“命中就换”？

一条查询进入优化器后，一次完整的异步物化视图透明改写大致经历六个阶段：

1. **候选剪枝与新鲜度仲裁（`MvRewritePreprocessor` + `MVTimelinessArbiter`）**：生产集群动辄存在上千个物化视图，绝不能把每个视图都丢进规则里两两匹配。预处理器先用“基表集合交集”做倒排索引过滤，再按相关性评分截断 Top-K 候选集；同时调用新鲜度仲裁器比对基表与 MV 的分区版本，立刻算出每个候选 MV 哪些分区新鲜、哪些分区已脏。
2. **表关系映射（`RelationMapping`）与等价类推导**：建立 $Q$ 中表扫描节点到 $V$ 中表扫描节点的双射或单射（注意自连接场景下同一张基表会出现多种合法排列，必须限制搜索扇出）。随后从等值连接条件 `a.k = b.k` 推导出列等价类（Equivalence Class），把不同表里的等价列归一到同一个代表元，避免因为查询写了 `WHERE a.k = 10` 而视图写了 `WHERE b.k = 10` 导致漏匹配。
3. **谓词拆分与蕴含判定（`PredicateSplit` + `RangeSimplifier`）**：把 $Q$ 和 $V$ 的过滤条件统一拆解为三部分——**等值谓词（Equal）**、**范围谓词（Range）**和**残余谓词（Residual）**。核心校验是证明 $P_Q \Rightarrow P_V$（即视图包含的数据是查询所需数据的超集），并求差算出需要补在 MV 扫描之上的最小**补偿谓词（Compensation Predicate）** $P_{\text{comp}}$，使得 $P_V \land P_{\text{comp}} \equiv P_Q$。例如 MV 存了 `dt >= '2026-01-01'`，查询查 `dt >= '2026-10-01' AND status = 1`，则只需在 MV 上补 `WHERE dt >= '2026-10-01' AND status = 1`。
4. **连接补偿与表达式映射（`EquationRewriter`）**：处理查询与视图表集合不一致的情况，再把查询输出列和残余表达式改写成对 MV 输出列的引用（支持线性代数等价替换，如 MV 存了 `a + 1`，查询要 `a + 2`，可通过表达式重写推导出来）。
5. **聚合上卷（Roll-up）与状态终结**：若查询分组键是视图分组键的真子集（$G_Q \subset G_V$），在 MV 上层补一个二次聚合算子（如 `SUM(mv_sum)`、`SUM(mv_cnt)`、`MIN(mv_min)`、`SUM(mv_sum) / SUM(mv_cnt)` 还原 `AVG`）。
6. **装入 Cascades Memo 与原计划同台算 Cost**：**命中的 MV 不一定更快！** 比如查询带有一个极高选择率的索引过滤条件，直接走基表主键点查只需 2 ms，而 MV 缺少该索引列反而要全表扫 200 ms。把改写后的子树与原始查询放进同一个 Memo Group 让 CBO 统一算 Cost 择优，是防止透明改写酿成性能回退的最后一道铁闸。

### 1.2 决定改写覆盖率的四把核心补偿武器

如果只支持“查询和视图表完全一样、分区完全新鲜”，真实业务里的透明改写命中率往往不到 10%。StarRocks、Calcite 与 Doris 之所以能用少量物化视图替换掉一整套 Druid / Kylin Cube 集群，靠的是四项硬核补偿机制：

1. **Query-Delta 补偿（查询比视图多表）**
   MV 定义为 `orders ⋈ customers`，用户查询 `orders ⋈ customers ⋈ products`。只要 MV 输出列里保留了与 `products` 连接所需的 `product_id`，优化器就能直接把 `orders ⋈ customers` 子树替换为 `MV_scan`，再往上接 `⋈ products`。这让公共事实-核心维表宽表能够无缝复用在各种新增业务维表的关联查询里。
2. **View-Delta 补偿（大宽表视图比查询多表——替换 Cube 的灵魂）**
   反过来，为了减少视图数量，数仓工程师常建一张大宽表 MV：`orders ⋈ customers ⋈ products ⋈ stores`，而某个看板只查 `orders ⋈ customers`。直觉上好像把 `products` 和 `stores` 丢掉就行，但在关系代数里这极其危险：
   - 如果 `products` 的连接键不唯一，大宽表 MV 里同一条订单会被**膨胀成多行**，导致 `SUM(amount)` 翻倍；
   - 如果某条订单在 `products` 里找不到匹配行（Inner Join），大宽表 MV 会把这条订单**悄悄过滤掉**，导致丢数。
   
   因此，StarRocks `MaterializedViewRewriter.compensateViewDelta()` 严格要求：只有在表上声明或推导出 **PK/FK 外键无损连接约束**（证明被多余连接的维表既唯一又总能匹配，或者通过 `LEFT JOIN` 连接唯一维表从而既不丢行也不膨胀行）时，才允许触发 View-Delta 改写。一句话：**没有 PK/FK 唯一性约束的大宽表，在严格语义下根本无法合法做 View-Delta 改写。**
3. **Outer Join 语义降级与空值拒绝（Null-Rejecting）**
   如果 MV 是 `A LEFT JOIN B`，用户查询是 `A INNER JOIN B`：MV 的输出是查询的超集（多了那些没匹配上 B、右列填 `NULL` 的左表行）。只要 MV 的输出列里包含一个在 B 表中天然 `NOT NULL` 的列（如 B 的主键或参与等值 `ON` 条件的列），优化器在 MV 上补一条 `WHERE b.pk IS NOT NULL` 即可无损降级为 `INNER JOIN`！但反过来，`INNER JOIN` 的 MV 永远不能回答 `LEFT JOIN` 查询；而且在构建等价类时，Outer Join 的 `ON` 谓词不能像 Inner Join 那样随意全局传导，必须经过 Null-Rejecting 严格判定，否则就会把本该保留的 `NULL` 补齐行误杀。
4. **Union 分区补偿（新鲜度与查询加速兼得）**
   异步 MV 的最大痛点是“基表刚写了今天的数据，MV 还没刷新完”。如果一发现基表有脏分区就粗暴放弃整个 MV，那么全天 24 小时持续入库的事实表上，异步 MV 的命中率几乎为零。
   **Union 补偿改写**彻底解开了这道死结：当用户查近 90 天指标、而 MV 的前 89 天分区新鲜且只有今日分区过期时，优化器自动构造出：
   ```sql
   SELECT region, SUM(amt)
   FROM (
       SELECT region, amt FROM mv_orders   WHERE dt < '2026-11-22'
       UNION ALL
       SELECT region, amt FROM base_orders WHERE dt = '2026-11-22'
   ) t
   GROUP BY region;
   ```
   89 天历史数据直走秒级预聚合 MV，仅今日增量回扫基表，最后在顶层由 `rewriteAggregatesForUnion()` 补一次 Roll-up 聚合，既保证了与直查基表完全一致的强实时结果，又吃满了历史预计算的加速红利。紧接着，`MVCompensationPruneUnionRule` 还会重新跑一遍谓词下推和分区裁剪——如果用户本来就只查历史 89 天，基表分支会被直接裁空消掉，退化为纯 MV 扫描。

### 1.3 同步 MV、异步 PCT MV 与行级 IVM MV 的改写分野

在阅读或设计系统时，切忌把三类物化视图的改写混为一谈：
- **同步物化视图（Sync MV / Rollup）**：仅支持单表，与基表在同一写入事务中强一致同步更新；在 StarRocks 中无需跨表匹配和分区新鲜度仲裁，直接由单表 `MaterializedViewSelector` 在索引选择阶段配合表达式、谓词与聚合状态改写完成。
- **异步分区级物化视图（Async PCT MV）**：支持多表 Join 与湖仓外表，靠分区级版本元数据（`MVTimelinessArbiter`）实现精准的过期分区判定与 `UNION ALL` 局部补偿，是当前工业界透明改写能力最完备的形态。
- **行级增量物化视图（Row-level IVM MV）**：虽然通过 TVR / DBSP 极大降低了刷新延迟（见前一篇 [从两个增量实验到云数仓内核]({{< relref "2026-11-22-incremental-computation-principles-and-engines.md" >}})），但它在透明改写上有一个尚未完全踏平的硬坎——基表发生局部行级修改但 IVM 任务尚未追平水位时，无法像分区级 PCT 那样简单用 `WHERE dt = today` 切出脏数据做 `UNION ALL` 补偿（必须引入带符号 Z-Set 增量流做实时合并读）。因此在当前 StarRocks 快照中，`MvRewritePreprocessor` 显式拒绝了 `refresh_mode = INCREMENTAL` 的透明改写，而在闭源云数仓中，BigQuery 与 Databricks 正是通过底层托管存储的变更流/版本化元数据来逐步打通“增量刷新 + 实时修补读”。

---

## 二、`AggState` 聚合状态组合代数：让复杂聚合彻底可上卷、可增量

Union 分区补偿、多级嵌套 MV（`分钟级 MV1 → 小时级 MV2 → 天级 MV3`）以及 Roll-up 改写，全部依赖一个隐含的数学前提：**聚合函数必须是可分解合并的（Algebraic / Associative）**。

`SUM`、`COUNT`、`MIN`、`MAX` 天然满足这个前提；但现实分析业务里占比极高的 `AVG`、`PERCENTILE_APPROX`、`HLL`、`BITMAP`、`MAX_BY`、`MIN_BY`、`ARRAY_AGG`、`MAP_AGG` 呢？如果 MV 里直接存 `AVG(latency)` 算出来的浮点数 `12.5`：
- 昨天 100 万条请求平均耗时 `10 ms`，今天 10 条请求平均耗时 `20 ms`，拿 `10` 和 `20` 根本算不出合并后的真实平均值！
- `PERCENTILE_APPROX(latency, 0.99)` 一旦输出成单一分位数标量，就彻底丢掉了内部的 T-Digest / KLL 草图结构，永远无法再合并。

### 2.1 四大 Combinator 代数：从 ClickHouse `-State/-Merge` 到 StarRocks PR `#62122`

ClickHouse 很早就用 `AggregatingMergeTree` 配合 `-State` / `-Merge` / `-MergeState` 后缀组合子（Combinator）给出了优雅的范式——**在物化视图里不存聚合终态结果，而是直接把聚合函数的二进制中间状态（`AggState`）持久化成一列**。StarRocks 进一步将这套状态代数与透明查询改写和增量计算（IVM）彻底打通，并在 PR `#62122`（`be/src/exprs/agg/combinator/agg_state_combine.h`）中补齐了关键的高性能组合子 `*_combine`：

![AggState 聚合状态组合代数，以及多列分区、Iceberg 分区变换与表达式 TTL 治理](/images/diagrams/mv-aggstate-combinators-and-partition-ttl.svg)

这套代数体系由四个各司其职的组合子构成：

| 组合子形态 | 函数类型 | 输入 → 输出契约 | 典型使用场景 | 性能与语义要点 |
| :--- | :--- | :--- | :--- | :--- |
| `{fn}_state(x)` | 标量函数 | 原始列 → 单行 `intermediate_type` | 极简写法配合 `{fn}_union` 入库 | **逐行装箱分配状态对象**，大批量写入时函数调用与内存分配开销偏高 |
| `{fn}_combine(x)`<br>（StarRocks `#62122`） | **聚合函数** | 原始列 → 分组聚合后的 `intermediate_type` | **MV1 物化写入 / IVM 增量预聚合** | 等价于 `fn_union(fn_state(x))`，但在底层直接调用 `_function->update()` 原地累加，彻底消除逐行临时状态构造与二次拷贝 |
| `{fn}_union(state)` | 聚合函数 | `intermediate_type` → `intermediate_type` | **嵌套 MV 上卷（MV1 → MV2）** | 只调用 `_function->merge()` 合并状态并继续序列化，**不触发 `finalize()`**，让下游 MV3 仍保有无限上卷能力 |
| `{fn}_merge(state)` | 聚合函数 | `intermediate_type` → 最终业务 `return_type` | **用户查询透明改写** | 合并状态并在末端调用 `finalize()` 吐出标准 SQL 结果（如 `DOUBLE` 均值或分位数） |

### 2.2 源码里的三个魔鬼细节：改写强制 Roll-up、参数保真与内存防漏

把 `AggState` 真正做进生产级优化器与执行引擎，远不止多注册几个函数名这么简单，源码里藏着三个极易踩坑的细节：

1. **即便 `GROUP BY` 键完全一致，也必须强制走 Roll-up 改写路径**
   在 StarRocks `AggregatedMaterializedViewRewriter.java` 中有一段非常精准的注释：
   > `// agg_state combinator cannot be used for non-rollup rewrite (convert agg_state to final result),`
   > `// so change isRollup to true if mv has agg_state combinator.`
   
   通常情况下，如果 MV 按 `(dt, region)` 聚合，用户查询也正好按 `(dt, region)` 聚合，改写器会走非上卷直读（Non-Rollup Rewrite）——直接把查询的 `Aggregate` 节点砍掉，投影 MV 列即可。可一旦 MV 列存的是 `avg_combine(amount)` 这种二进制中间状态，砍掉 `Aggregate` 就会把一坨裸二进制状态吐给用户！因此只要检测到 MV 包含 `AggStateUnionCombinator` 或 `AggStateMergeCombinator` 相关状态列，改写器必须强行置 `isRollup = true`，在 MV 扫描之上套一层 `avg_merge(mv_col)` 完成最终反序列化与 `finalize()`。
2. **辅助常量参数保真（StarRocks `#83385`）**
   对于 `percentile_approx(x, 0.99, 2048)`、`max_n(x, 10)` 这类带常量参数的聚合函数，常量参数 `n` 或 `compression` 并不在每条数据行里，而是影响中间态合并时的截断大小与精度。序列化状态元数据 `AggStateDesc` 必须把这些辅助常量参数完整绑定并透传到 `_union` 与 `_merge` 阶段，否则跨分区合并时就会丢失截断阈值或退化为默认精度。
3. **包装状态的析构与版本演进（StarRocks `#81054`）**
   很多复杂聚合状态（如 `HLL`、`Bitmap`、`T-Digest`、`ArrayAgg`）内部在堆上动态分配了二级内存块；当 `AggStateCombinator` 做批量销毁（`batch_destroy`）时，必须把销毁动作穿透给内层被包装的 `AggregateFunction`，否则每跑一次查询就会发生隐蔽的堆内存泄漏。同时，把引擎内部聚合状态持久化到磁盘意味着**二进制格式成为长期存储契约**——未来内核升级如果改了 `T-Digest` 或 `HashSet` 的内存序列化布局，必须做好版本号兼容，否则老分区数据将无法反序列化读取。

---

## 三、湖仓分区治理：多列分区、Iceberg 变换与表达式 TTL

解决了“怎么改写”和“怎么存状态”，接下来就要面对湖仓一体场景里最琐碎、却最容易引发数据错漏的元数据时效性层：**如何精确地用最小刷新代价维护物化视图，并安全清理过期数据？**

### 3.1 多列分区（PR `#52577`）与 Iceberg 分区变换的单调性边界

在早期 OLAP 引擎里，异步物化视图只支持基于单个时间列（如 `date_trunc('day', dt)`）的 1:1 或 N:1 分区映射。但真实企业数仓（Hive / Iceberg / StarRocks 内表）中，多列复合分区比比皆是——例如基表按 `(dt, region)` 或 `(dt, tenant_id)` 划分，而 Iceberg 表更是普遍使用 **Hidden Partitioning Transform**（`year(ts)`、`month(ts)`、`day(ts)`、`hour(ts)`、`truncate(code, 4)`、`bucket(user_id, 16)`）。

为了支持精准的**分区变更追踪（Partition Change Tracking，PCT）**——即基表只改动了 `(2026-11-22, 'ap-east')` 这一个分区时，MV 只重刷对应的那一个分区，且改写时其他几千个分区照常命中——StarRocks 在 PR `#52577`（`MVPartitionExprResolver`、`MVTimelinessListPartitionArbiter`、`IcebergPartitionUtils`）里把分区映射抽象成了一条清晰的数学红线：**单调保序映射（Monotonic Partition Mapping）**。

- **保序单调变换（可建立精确 1:1 / N:1 分区依赖）**：
  基表按 `(day(ts), region)` 分区，MV 按 `(month(ts), region)` 上卷分区。由于 `day(ts) → month(ts)` 是单调满射（每个月分区严格覆盖 28–31 个连续天分区），且 `region → region` 是恒等映射，仲裁器可以严格证明：`(2026-11-22, 'CN')` 的数据变更**绝对不会**影响 `(2026-10, 'CN')` 或 `(2026-11, 'US')`。在刷新与改写时，范围分区与列表分区统一归一化成标准区间/集合表达式，直接锁定受影响的精确 MV 分区子集。
- **非单调 / 哈希变换（无法局部区间追踪）**：
  如果 Iceberg 基表按 `bucket(user_id, 16)` 分区，而 MV 按 `dt` 分区——同一个 `bucket` 里散落着所有日期的数据，改了一个 `bucket` 可能波及 MV 的全部 365 个日期分区！此时 PCT 无法建立局部依赖，系统必须在建表/刷新规划阶段诚实判定“不可分区级追踪”，要么要求 MV 保留同阶 `bucket` 列，要么降级为全量刷新，或者交由行级增量引擎（TVR / IVM）在文件/行级消化变更。

### 3.2 表达式 TTL（PR `#53488`）与嵌套物化视图的“防误删护栏”

多列分区一旦落地，传统固定数量的生命周期配置 `partition_ttl_number = 30` 立刻形同虚设：按 `(dt, region)` 分区且有 50 个 region 时，保留 30 个分区意味着连完整 1 天的数据都存不下；而且业务常常需要差异化留存策略，例如“普通区保留 30 天、测试区只保留 3 天、核心财务区永久保留”。

StarRocks 在 PR `#53488` 中引入了**表达式 TTL（`partition_retention_condition`）**，允许用户直接用标准 SQL 布尔表达式定义保留窗口：

```sql
PROPERTIES (
  "partition_retention_condition" = "dt >= current_date() - INTERVAL 30 DAY AND region != 'sandbox'"
)
```

在实现上，`PartitionSelector.getExpiredPartitionsByRetentionCondition()` 并没有另起炉灶写一套简陋的表达式求值器，而是**直接复用了 CBO 优化器的分区裁剪器（`PartitionColPredicateEvaluator` / `OptOlapPartitionPruner`）**：把所有现有分区的边界元数据喂进裁剪器求反集，精准筛选出不再满足保留条件的过期分区执行异步删除。

这里隐藏着一个极容易在生产上酿成事故的**嵌套级联陷阱**：
设想一条两级物化视图链路——明细级 `MV1` 体积庞大，配置表达式 TTL 仅保留最近 **7 天**；汇总级 `MV2` 建在 `MV1` 之上，用于支撑年度趋势报表，配置保留 **365 天**。第 8 天凌晨，`MV1` 的 TTL 调度器正常淘汰掉了 8 天前的分区。如果下游 `MV2` 的刷新仲裁器把“`MV1` 少了一个历史分区”误判成“上游基表把 8 天前的数据删掉了”，就会立刻触发级联清理，把 `MV2` 里辛辛苦苦攒了 365 天的历史年报抹得一干二净！

正确的工程护栏必须把**“TTL 滚动归档”**与**“业务数据真删”**在刷新契约上解耦：下游 MV 仅在其自身保留窗口与上游活跃窗口的交集内触发增量分区对齐（或者在刷新调度中对上游历史淘汰分区启用保护策略 / `excluded_trigger_tables`），确保历史汇总资产绝不被上游短周期 TTL 误杀。

也正是“SPJG View-Delta 改写 + `AggState` 组合子 + 多列分区/表达式 TTL”这三驾马车齐备之后，现代湖仓引擎才真正具备了**全面替换 Druid / Kylin / 预计算 Cube** 的底气：不再需要预先穷举 $2^d$ 种维度组合把存储撑爆，只需建少数几张核心宽表与高复用聚合 MV，靠 Roll-up、View-Delta 和 Union 补偿动态覆盖海量即席与固定报表查询。

---

## 四、物化视图自动推荐（MV Advisor）：从 Query Log 到云费用 ROI 闭环

即便引擎的透明改写、增量刷新和分区治理都做到了极致，在大型云数仓（如 MaxCompute、BigQuery、Redshift）上依然卡着“最后一公里”：**一个拥有上万张表、每天跑几百万条 SQL 的租户，到底该建哪 20 个物化视图？**

人工拍脑袋建视图通常会陷入两个极端：要么建得太保守，80% 的重复扫描算力仍在白白浪费；要么业务各自为战建了几百个高度重叠的视图，不仅拖慢了优化器每次编译的候选匹配耗时，刷新账单还反超了查询省下来的费用。

从早期微软的 **AutoAdmin**，到今天云上的 **BigQuery Materialized View Recommender**、**Amazon Redshift AutoMV** 与 **阿里云 MaxCompute 物化视图推荐**，业界已经收敛出一套四阶段工程闭环。

![云数仓物化视图自动推荐闭环：Query Log 挖掘、What-If 改写核验与云成本 ROI 背包优化](/images/diagrams/mv-auto-recommendation-roi-pipeline.svg)

### 4.1 四阶段推荐流水线与三个必避陷阱

1. **阶段一：海量 Query Log 挖掘与规范化（Normalization）**
   直接按 SQL 文本分组毫无意义——仅仅日期常量不同（`'2026-11-21'` vs `'2026-11-22'`）或表别名不同（`t1` vs `o`）就会把同一类查询切碎。采集层必须先做 AST / 逻辑计划规范化：剥离字面量、统一表别名与谓词顺序、抽取规范化的 **SPJG 子图签名**（参与表集合、连接图与连接类型、分组键集合、聚合函数集合、过滤维度列），同时关联上每条查询的执行频次 $f_q$、历史扫描字节与 CU（Compute Unit）账单，以及每张基表的 DML 写入频次 $\lambda_{\text{DML}}$。
2. **阶段二：候选生成与蕴含合并（Subsumption Merging）**
   如果发现高频查询 $Q_1$ 按 `(dt, region, channel)` 聚合、$Q_2$ 按 `(dt, region)` 聚合、$Q_3$ 只查 `region = 'CN'`，绝不能傻傻地推荐三个独立 MV。合并器利用改写器的蕴含关系，把相似子图向上泛化为一个**公共覆盖祖先候选 MV**：按 `(dt, region, channel)` 预聚合、把过滤维度上提为分区键或普通列、把 `AVG`/`PERCENTILE` 自动升级为 `AggState` 组合列，从而用 **1 份刷新与存储成本同时吃掉 $Q_1, Q_2, Q_3$ 三类查询**。
3. **阶段三：What-If 优化器改写核验 + 云成本 ROI 背包求解**
   这是推荐系统最容易“翻车”的核心关卡——**绝不能让推荐器和线上改写器成为“两张皮”**。离线启发式算法觉得能覆盖 50 条 SQL 的候选视图，真建出来后可能因为缺 PK/FK 约束、隐式类型转换或时区非确定函数导致线上 CBO 根本改写不中。
   工业界硬核做法是引入 **What-If 虚拟目录校验**：在推荐器内挂载与线上同版本的真实 CBO 优化器，把虚拟候选 MV 注册进内存 Catalog，逐一重放典型历史查询验证：**① 能否真正改写命中；② 命中后 CBO 估算代价是否显著低于原计划**。只把通过双重校验的查询纳入有效覆盖集 $\text{Cov}(m)$。
   
   随后把视图选择建模为带存储容量上限 $S_{\max}$ 与刷新预算上限 $R_{\max}$ 的**云费用净收益背包问题（Knapsack / ILP）**：

   $$\text{NetROI}(m) = \sum_{q \in \text{Cov}(m)} f_q \cdot \Big(\text{CU}(q) - \text{CU}(q \mid m)\Big) - \text{CU}_{\text{refresh}}\big(m, \lambda_{\text{DML}}, \text{mode}\big) - \alpha \cdot \text{Cost}_{\text{storage}}(m)$$

   这里必须处理**视图间收益重叠（次模性，Submodularity）**：候选 `MV1` 和 `MV2` 单独看都能覆盖同一批 100 条高耗 SQL，独立打分分列第一、第二，可一旦先选中 `MV1`，这 100 条 SQL 已经被加速了，`MV2` 的边际增益会瞬间跌到几乎为零！因此求解器必须采用**边际收益密度贪心（每次选入最优 MV 后立刻扣减已被覆盖的收益，动态重算剩余候选的 $\Delta\text{NetROI} / \text{Cost}$）**或小规模整数线性规划（ILP），并设置单表关联 MV 上限以保护线上编译延迟。
4. **阶段四：影子灰度、持续计量与僵尸视图自愈淘汰**
   线上业务团队对“系统自动建表扣费”天然充满警惕。成熟的推荐平台（如 BigQuery Recommender、MaxCompute 智能物化视图、Redshift AutoMV）普遍分两档交付：
   - **顾问模式（Shadow / Advisory）**：输出可审计的 DDL、预计覆盖查询数、预计月度净省 CU 与预计刷新开销，支持一键建表；
   - **全托管自愈模式（AutoMV）**：灰度开启自动建表后，控制面持续监控每个托管 MV 的真实线上改写命中率与 `查询节省 CU / 刷新消耗 CU` 比值。一旦业务下线或负载漂移导致连续 $K$ 天净收益转负，自动将视图降级为暂停刷新直至静默回收，彻底根除“建了没人管”的僵尸视图顽疾。

---

## 五、开源与闭源云数仓全景横评

把开源引擎与主流闭源云数仓放在同一张坐标纸上对照，能清晰看出不同产品基因下的战略取舍：

| 平台 / 引擎 | 透明改写能力 | 聚合状态组合（AggState） | 分区与湖仓治理 | 自动推荐与自治闭环 | 核心定位与取舍 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **StarRocks** | **极强**：SPJG 全量支持（Query/View-Delta、Outer Join、Union 补偿、Roll-up、嵌套 MV） | 原生完整四元组合子（`_state`/`_combine`/`_union`/`_merge`，PR `#62122`）+ 透明改写自动终结 | 多列分区（`#52577`）、Iceberg 变换适配、表达式 TTL（`#53488`）、Hive/Iceberg/Paimon 湖仓 PCT & IVM | 侧重规则可观测（`TRACE REASON MV`）与人工建模，外部诊断工具演进中 | 开源界**综合改写深度 + 湖仓集成**天花板，非常适合替代 Druid/Kylin Cube |
| **Apache Doris** | **强**：Nereids 架构下支持 SPJG、Roll-up、TopN/Window/Limit 改写与 Union 补偿 | 支持 `AGG_STATE` 类型与 `_state`/`_union`/`_merge` | MTMV 分区级追踪与湖仓接入，行级 Table Stream IVM 演进中 | 以手动创建与可观测诊断为主 | 与 StarRocks 在开源 OLAP 赛道并驾齐驱，Nereids 规则工程化规整 |
| **Apache Calcite** | **算法标尺**：`MaterializedViewRule` 完整实现 Goldstein & Larson 论文核心 | 依赖宿主引擎扩展自定义聚合与 Roll-up 规则 | 仅提供逻辑优化框架，不含物理存储与分区元数据 | 无内置运维态推荐器 | 众多开源引擎（Hive/Flink/Spark+Calcite 定制架构）的改写算法蓝本 |
| **ClickHouse** | 弱（传统上依赖显式查表或简单投影 Projection，非全功能多表 SPJG 透明改写） | **组合子鼻祖**：`AggregatingMergeTree` + `-State`/`-Merge`/`-MergeState` 生态最全 | 写入触发增量物化（本质上是插入触发器），多表 Join 语义较弱 | 无全自动多表 MV 推荐 | 极致写入期预聚合吞吐，但把 Join 与改写控制权留给开发者显式建模 |
| **Google BigQuery** | **强（Smart Tuning）**：全托管自动把查询改写至 MV，支持聚合上卷与增量基表合并读 | 内置 `HLL_COUNT.INIT/MERGE` 等草图函数辅助复杂去重 | 托管列存与分区对齐，复杂多表更新下自动回退重算 | **成熟**：每日自动基于历史扫描生成 **MV Recommender** 建议与字节节省预估 | Serverless 按扫描字节计费基因决定了它把 Smart Tuning 与自动推荐做得最顺滑 |
| **Amazon Redshift** | **强（Automatic Query Rewrite）**：支持透明改写与精细新鲜度门禁（`SYS_MV_AQMV_HISTORY`） | 侧重常规可加聚合与增量刷新 | 托管本地/RA3 分区存储与自动刷新调度 | **成熟（AutoMV）**：自动监测负载创建、刷新并在负收益时自动淘汰 | 企业数仓实用主义标杆，强调自动化同时保留清晰审计视图 |
| **Snowflake** | **克制**：传统 MV 仅限单表且无 Join；复杂多表管线全面转向 **Dynamic Tables** 显式查询 | 提供 `BITMAP_AGG` / `HLL_ACCUMULATE` 等显式状态函数 | Dynamic Tables 声明式 `TARGET_LAG` 全自动编排 DAG | 偏向声明式建模与自动聚类/搜索优化，不押注黑盒多表透明改写 | 刻意规避复杂多表透明改写的不确定性，用显式声明式数据管线换取可预测 SLA |
| **Databricks** | 演进中（Unity Catalog MVs + Photon 加速） | 结合 Delta/Enzyme 增量状态维护 | 深度绑定 Delta Lake / Liquid Clustering；提供 `EXPLAIN CREATE MV` 事前校验增量资格 | 强调开发者事前可验证性，而非全黑盒自动改写 | 湖仓工程化路线：先把增量刷新与 Photon 性能打透，再逐步扩展透明改写 |
| **阿里云 MaxCompute** | 面向超大规模批/湖仓的 SPJG 透明改写与分区/增量融合 | 支持复杂聚合改写与增量中间状态协同 | EB 级多级分区、CDC 增量构建与多租户资源隔离 | **核心发力方向**：依托超大规模多租户 Query Log + CU 账单驱动推荐、改写与增量降本闭环 | 在超大规模按量/CU 计费场景下，“增量计算 + 透明改写 + 自动推荐”直接等价于真金白银的算力降本 |

---

## 六、架构落地时的四条铁律

如果今天你要为一个湖仓或 AI 数据平台设计或引入物化视图能力，以下四条落地红线值得贴在白板最上方：

1. **先画清“加速”与“建模”两条线**：面向 BI/即席查询透明加速（不改 SQL），必须把 **SPJG 补偿（View-Delta / Union）+ CBO Cost 竞价**做扎实；面向复杂多跳 ETL 数据加工，学 Snowflake Dynamic Tables 走显式声明式增量表，不要强求所有复杂 SQL 都能被黑盒改写。
2. **用 `AggState` 而不是特判代码解决复杂聚合**：与其在优化器里为 `AVG`、`STDDEV`、`PERCENTILE`、`MAX_BY` 写几十条硬编码特判，不如统一建立 `_combine / _union / _merge` 状态组合代数，并从第一天起把状态版本号与辅助常量参数写进元数据契约。
3. **湖仓分区映射宁可保守回退，绝不静默错数**：只对严格可证明单调保序的分区表达式与 Iceberg 变换放行 PCT 局部刷新与 Union 改写；遇到多列分区尽早用表达式 TTL 取代固定分区数，并在嵌套 MV 链路上隔离 TTL 淘汰与基表删除语义。
4. **推荐系统必须与线上 CBO 共享同一颗“大脑”**：没有 What-If 优化器真实改写核验、没有把基表 DML 刷新 CU 写入 ROI 公式、没有僵尸视图自动下线机制的推荐器，上线半年后大概率变成集群里最大的算力黑洞。

---

## 源码基线与参考资料

- **本地核验源码快照**：
  - StarRocks（`a9a9fd7`）：`fe/fe-core/.../rule/transformation/materialization/MaterializedViewRewriter.java`、`AggregatedMaterializedViewRewriter.java`、`MVCompensationPruneUnionRule.java`、`be/src/exprs/agg/combinator/agg_state_combine.h`、`fe/fe-core/.../connector/iceberg/IcebergPartitionUtils.java`、`fe/fe-core/.../rule/transformation/partition/PartitionSelector.java`；关键历史提交 `#52577`（`2db7d69`）、`#53488`（`b34b61e`）、`#62122`（`b7380d3`）、`#81054`（`e8f363f`）、`#83385`（`7dd4de4`）。
  - Apache Calcite（`89aa632`）：`core/src/main/java/org/apache/calcite/rel/rules/materialize/MaterializedViewRule.java`。
  - Apache Doris（`5ff9558`）：`fe/fe-core/.../nereids/rules/exploration/mv/AbstractMaterializedViewAggregateRule.java`、`RelationMapping.java`。
  - ClickHouse（`c824bb6`）：`src/AggregateFunctions/Combinators/AggregateFunctionMerge.h`、`AggregateFunctionState`。
- **经典论文与云数仓公开资料**：
  - Jonathan Goldstein & Per-Åke Larson, *Optimizing Queries Using Materialized Views: A Practical, Scalable Solution*, SIGMOD 2001.
  - Sanjay Agrawal, Surajit Chaudhuri & Vivek Narasayya, *Automated Selection of Materialized Views and Indexes in SQL Databases* (AutoAdmin), VLDB 2000.
  - Google Cloud BigQuery Documentation: *Smart Tuning* & *Materialized View Recommender*；AWS Redshift Documentation: *Automatic Query Rewriting (`SYS_MV_AQMV_HISTORY`)* & *Automated Materialized Views (AutoMV)*；Snowflake Dynamic Tables & `BITMAP_AGG`；Databricks Unity Catalog Materialized Views & `EXPLAIN CREATE MATERIALIZED VIEW`；阿里云 MaxCompute 物化视图与智能推荐文档。

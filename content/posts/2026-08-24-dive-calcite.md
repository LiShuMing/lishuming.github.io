---
title: "【源码】深入 Calcite 优化器：Hep、Volcano、谓词推导与 Join Reorder"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
slug: "dive-calcite-optimizer"
categories:
  - 数据库
tags:
  - Apache Calcite
  - 查询优化器
  - CBO
  - Join Reorder
  - StarRocks
  - Apache Doris
description: "结合 Apache Calcite、StarRocks 与 Apache Doris 源码，分析 Hep、Volcano、Memo、Null-Rejecting 谓词推导、Outer Join Reorder、物理计划后处理、运行时统计与 Dynamic Filter 的实现边界。"
draft: false
---

Apache Calcite 经常被称为“通用 SQL 优化器”，但这个称呼容易造成两个误解：第一，Calcite 不是把 SQL 输入后直接输出分布式执行计划的完整数据库内核；第二，接入 Calcite 也不意味着自动获得成熟的 Join Reorder、Runtime Filter 和自适应执行能力。

更准确地说，Calcite 提供了一套可嵌入的关系代数、元数据、Trait、规则匹配与代价搜索框架。Flink、Hive、Druid 等系统可以在这套框架上定义自己的关系节点、物理 Convention、Metadata 和 Rule，再把优化结果翻译成各自的执行计划。它的抽象层次较多，学习曲线确实高，但这些抽象主要服务于“让不同引擎复用框架，同时保留自己的语义和物理实现”。

本文不做 Calcite API 罗列，而是从五个实际问题切入，并使用 StarRocks、Apache Doris 的自研优化器作为对照：

1. Null-Rejecting 条件如何推导出 `IS NOT NULL`，并把 Outer Join 转换为 Inner Join？
2. Left Outer Join 为什么不能随意换序，Calcite 如何枚举合法 Join Order？
3. Hep 与 Volcano 分别解决什么问题，为什么成熟优化器通常同时需要 RBO 与 CBO？
4. CBO 选出物理计划后，为什么还要再做一轮物理计划优化？
5. 运行时统计与 Dynamic Filter 属于优化器还是执行器，Calcite 的能力边界在哪里？

## 核心结论

1. **Hep 与 Volcano 不是新旧两代优化器，也不是二选一。** Hep 适合按阶段执行确定性的规范化和收敛式改写；Volcano 适合在等价表达式和不同物理 Trait 之间保留候选，并按代价选择实现。
2. **谓词推导、Outer Join 消除和 Join Reorder 是同一条链路。** Null-Rejecting 条件越早被识别，Outer Join 越可能收紧为 Inner Join，后续 Join Reorder 的合法搜索空间就越大。
3. **Left Join Reorder 的核心不是“代价是否更低”，而是“变换是否语义等价”。** CBO 只能在合法候选中比较成本；关联律、交换律、Null-Generating Side 和谓词引用范围共同决定候选是否合法。
4. **物理计划后处理是必要的，但必须控制职责。** Runtime Filter 生成、局部 Projection 合并、Shuffle Key 裁剪等依赖最终物理拓扑，适合在 CBO 后执行；大范围 Join Order 或 Distribution 改写如果不重新计价，会破坏 CBO 的最优性假设。
5. **Calcite Core 没有提供开箱即用的分布式 Runtime Filter 和运行时重优化闭环。** 它提供 Metadata、Planner Context 和可组合 Program，具体引擎可以注入历史统计或重新调用优化器；但过滤器的 Build、Merge、传输、等待和 Scan 下推属于执行引擎。
6. **Calcite 的价值是通用扩展性，StarRocks 与 Doris 的价值是垂直整合。** 前者用更多抽象换 Adapter 可插拔性；后两者让统计、物理属性、Runtime Filter、Pipeline 和存储下推共享同一套产品语义。

### 源码分析基线

| 项目 | 源码快照 | 日期 | 重点范围 |
|------|----------|------|----------|
| Apache Calcite | [e8e0dd5](https://github.com/apache/calcite/tree/e8e0dd54145c44f61b73acad1ffb96c14bddff78) | 2026-05-04 | Hep、Volcano、Predicate Metadata、MultiJoin、HyperGraph/DPHyp |
| StarRocks | [0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 2026-03-30 | QueryOptimizer、Join Predicate Pushdown、Outer Join Reorder、Physical Rewrite |
| Apache Doris | [5202d06](https://github.com/apache/doris/tree/5202d06dd8feb3390ff32839227eeee89c345b57) | 2026-08-21 | Nereids、Null 推导、DPHyp、HBO、Runtime Filter Post Process |

三个项目都在快速演进。本文结论以这些源码快照为准，尤其 Calcite 的 HyperGraph/DPHyp 仍标记为 Experimental，不应外推为所有 Calcite 应用的默认执行路径。

本文使用的 Calcite 提交位于 1.41.0 之后、1.42.0 正式发布之前。Apache Calcite [1.42.0 发布记录](https://calcite.apache.org/docs/history.html#1420--2026-05-31) 显示该稳定版发布于 2026-05-31，因此本文应被理解为对一个确定 Git Snapshot 的源码分析，而不是“1.42.0 所有发行包都具有完全相同默认行为”的产品说明。Rule 是否注册、Program 如何编排以及 Adapter 覆盖了哪些 Metadata，仍由具体集成系统决定。

## Calcite 的整体设计：先建立正确的心智模型

### 从 SQL 到可执行计划

Calcite 的典型规划链路可以抽象为：

```text
SQL
  → SqlNode：解析后的 SQL AST
  → Validator：名称解析、类型推导、语义检查
  → RelNode + RexNode：关系代数与标量表达式
  → HepProgram：规范化、消除、下推等确定性改写
  → VolcanoPlanner：逻辑等价探索、Trait 转换、物理实现与 Cost 搜索
  → Physical RelNode
  → 第二阶段局部物理改写
  → Adapter/Engine Translator
  → 引擎自己的执行计划
```

这里有四组概念需要分开：

| 概念 | Calcite 表示 | 解决的问题 |
|------|--------------|------------|
| 关系算子 | `RelNode` | Scan、Filter、Project、Join、Aggregate 等关系结构 |
| 标量表达式 | `RexNode` | 列引用、常量、函数、谓词及三值逻辑 |
| 物理要求 | `RelTraitSet` | Convention、Distribution、Collation 等输出性质 |
| 优化依据 | `RelMetadataQuery` | RowCount、Selectivity、Cost、Predicates、Unique Keys 等元数据 |

这种拆分看似繁琐，却解决了通用框架最困难的问题：同一个逻辑 Join 可以有 Enumerable、JDBC 或某个计算引擎自己的物理实现；同一种语义可以在不同 Distribution 和 Collation 下形成不同候选；统计信息也不必写死在关系节点中。

### Hep：可编排的启发式重写

[`HepPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) 是 `RelOptPlanner` 的启发式实现。它把计划保存成单根 DAG，通过 Digest 合并等价节点，并按 [`HepProgram`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgram.java) 中的指令执行规则。

[`HepProgramBuilder`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java) 可以控制：

- Rule Instance、Rule Collection 或 Rule Class；
- 多条规则组成的 Group；
- Top-Down、Bottom-Up、Depth-First 等 Match Order；
- 匹配次数上限或执行到 Fixpoint；
- 子 Program、Converter Rule 和 Common Subexpression Rule；
- 是否把相同表达式共享为 DAG。

因此 Hep 的重点不是“找全局最低代价”，而是**精确控制规则何时、按什么顺序、执行多少次**。以下任务通常适合 Hep：

```text
Subquery Decorrelation
  → 常量折叠与表达式规范化
  → Filter/Project 合并
  → Predicate Pushdown
  → Outer Join Simplification
  → 无效算子消除
```

规则顺序具有工程价值。例如先识别 Null-Rejecting Filter，再消除 Outer Join，最后做 Join Reorder，通常比把三类规则无序混在一起更容易收敛，也更容易调试。

Hep 的限制同样明确：它可以不断生成“更规范”的树，却不会自动保留同一 Group 下的多个物理候选并比较完整代价。规则局部看起来更好，不代表组合后全局最优。

### Volcano：等价类、Trait 与 Cost 的统一搜索

[`VolcanoPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) 使用动态规划式搜索管理候选计划。它的核心不是一棵不断被覆盖的树，而是两层等价集合：

```text
RelSet
  同一关系语义的所有表达式
  ├── RelSubset[Convention=NONE, Distribution=ANY]
  ├── RelSubset[Convention=ENGINE, Distribution=HASH(k)]
  └── RelSubset[Convention=ENGINE, Distribution=SINGLE]
           ├── Candidate A
           ├── Candidate B
           └── 当前最低代价表达式
```

- `RelSet` 表示逻辑等价的关系表达式；
- [`RelSubset`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java) 在等价语义上进一步限定相同 Trait；
- Transformation Rule 生成逻辑等价表达式；
- Implementation/Converter Rule 生成物理实现或 Trait 转换；
- Metadata 与 `RelOptCost` 计算候选成本；
- 子节点成本变化会向父节点传播，最终从 Root Subset 提取最便宜计划。

这与 Cascades 风格 Memo 高度相似。StarRocks 和 Doris Nereids 的 `Memo → Group → GroupExpression`，都在解决相同问题：**按语义等价类共享子计划，并在需要的物理属性下记录最优实现。**

Volcano 也不是“注册规则后自然得到最优计划”。最终质量仍取决于 Rule 是否生成完整候选、Trait 是否表达真实物理性质、Metadata 是否可靠，以及 Cost Model 是否反映网络、内存、并发和算子实现。

### Metadata 是规则之间的事实总线

Calcite 通过 [`RelMetadataQuery`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMetadataQuery.java) 查询 Row Count、Selectivity、Unique Keys、Collation、Distribution 和 Pulled-up Predicates。它把基础统计、关系推导、规则判断与 Cost Model 解耦：

```text
Table/Adapter 提供基础统计
         ↓
Metadata Handler 沿 RelNode 推导
         ↓
Rule 判断是否可变换
         ↓
Cost Model 比较物理实现
```

代价是调用链较隐蔽：读一条 Rule 往往看不到完整逻辑，必须继续追踪 Metadata Provider 和 Handler。对于单一数据库内核，这种间接层可能显得冗余；对于多 Adapter 框架，它是扩展性的核心。

## Null-Rejecting：从谓词推导到 Outer Join 消除

### SQL 三值逻辑才是变换依据

考虑：

```sql
SELECT a.id, b.score
FROM a
LEFT JOIN b ON a.id = b.id
WHERE b.score > 10;
```

Left Join 为未匹配的 `a` 行补出 `b.score = NULL`。`NULL > 10` 的结果是 `UNKNOWN`，而 `WHERE` 只保留 `TRUE`，所以这个 Left Join 可以安全收紧为 Inner Join。

真正需要证明的是：**当某一侧相关列替换为 NULL 时，谓词是否一定不可能为 TRUE。** 这就是 Null-Rejecting 判断。

### Calcite：Strong、FilterJoinRule 与 Predicate Metadata

Calcite 把这条链路拆成三种能力。

第一，[`Strong`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java) 为不同 `SqlKind` 定义 Null 传播策略，`Strong.isNotTrue(expr, nullColumns)` 判断指定列为 NULL 时表达式是否必然为 NULL 或 FALSE。

第二，[`RelOptUtil.simplifyJoin()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelOptUtil.java) 检查左右 Null-Generating Side；[`FilterJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java) 在 Smart 模式下先收紧 Join Type，再把 Filter 分类到 Join 或左右输入。

第三，[`RelMdPredicates`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java) 从子节点谓词和 Join 等值关系推导两侧谓词；[`JoinPushTransitivePredicatesRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinPushTransitivePredicatesRule.java) 再把结果变成 Filter。

此外，[`JoinDeriveIsNotNullFilterRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinDeriveIsNotNullFilterRule.java) 会对 Inner Join Condition 引用的 Null-Rejecting Key 生成 `IS NOT NULL`，既缩小输入，也减少大量 NULL Key 进入 Hash Join 后造成的数据倾斜。

```text
Strong 判断 Null-Rejecting
  → simplifyJoin 收紧 Join Type
  → classifyFilters 决定合法下推方向
  → RelMdPredicates 沿等值关系推导
  → JoinPushTransitivePredicatesRule 创建 Filter
```

### Semi/Anti Join 为什么不能统一推导

| Join 类型 | 输出哪一侧 | `IS NOT NULL` 的安全边界 |
|-----------|------------|--------------------------|
| Inner Join | 左右匹配行 | 普通等值条件下，两侧 NULL Key 都无法匹配 |
| Left Semi Join | 左侧匹配行 | Probe NULL 通常无匹配；Build NULL 也不贡献普通等值匹配 |
| Left Anti Join / `NOT EXISTS` | 左侧未匹配行 | Probe NULL 可能正是未匹配结果，过滤它可能改变语义 |
| Null-Aware Anti / `NOT IN` | 受 Build NULL 影响 | 必须保留 UNKNOWN/NULL 语义，不能按普通 Anti 处理 |

Calcite 当前实现体现了这种不对称：

- `JoinDeriveIsNotNullFilterRule` 默认只匹配 Inner Join；
- `RelMdPredicates` 对 Semi Join 可以沿两侧推导，但只 Pull Up 左侧输出语义；
- 对 Anti Join 主要保留左侧已知谓词，并严格限制推导方向；
- `FilterJoinRule` 明确禁止把 Anti Join 的 ON 条件随意推入左右输入。

因此，从 Semi/Anti Join 条件统一推导 Not Null 不是安全规则，必须同时考虑输出侧、普通 Equality 与 Null-Safe Equality，以及 `NOT EXISTS` 和 `NOT IN` 的差异。

### StarRocks：在 JoinPredicatePushdown 中闭环

StarRocks 将更多逻辑集中在 [`JoinPredicatePushdown`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java)：

1. `rangePredicateDerive()` 和 `equivalenceDerive()` 生成范围及等价谓词；
2. `deriveIsNotNullPredicate()` 从等值 Join Key 构造冗余 Not-Null；
3. Inner Join 向两侧推导，Right Semi 向左侧推导，Left Semi 向右侧推导；
4. Not-Null 同时保存到 `OptimizerContext`，供上层 Join 使用；
5. `convertOuterToInner()` 通过 `Utils.canEliminateNull()` 或已下推 Not-Null 收紧 Outer Join。

Full Outer Join 的处理尤其清楚：

```text
左右两侧都 Null-Rejecting  → Inner Join
只过滤左侧补 NULL 行       → Left Outer Join
只过滤右侧补 NULL 行       → Right Outer Join
两侧都不能证明             → Full Outer Join
```

### Doris Nereids：Slot 替换为 NULL 后常量折叠

Doris 的 [`ExpressionUtils.inferNotNullSlots()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/util/ExpressionUtils.java) 采用直接方法：

1. 把目标 Slot 替换为对应类型的 `NULL Literal`；
2. 调用 `FoldConstantRule.evaluate()`；
3. 结果为 NULL 或 FALSE，则该 Slot 对当前谓词是 Null-Rejecting。

代码还限制表达式宽度、深度与参与 Slot 数量，避免规划时间爆炸。[`InferFilterNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferFilterNotNull.java) 为 Filter 生成 Not-Null；[`InferJoinNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferJoinNotNull.java) 处理 Inner/Semi Join；[`EliminateOuterJoin`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java) 收紧 Outer Join。

Outer Join 转为 Inner Join 后，Doris 还从等值条件继续生成两侧 `IS NOT NULL`，使更深层 Outer Join 能在后续 Fixpoint 中递归消除。

## Left Join Reorder：先证明合法，再比较代价

### 为什么普通交换律不成立

Inner Join 在满足条件时具有交换律和结合律，但 Left Join 保留左侧未匹配行并在右侧补 NULL：

```text
A LEFT JOIN B ≠ B LEFT JOIN A
```

重排是否安全还取决于 Preserved Side、Null-Generating Side、上层谓词是否拒绝 NULL、Join Condition 引用范围、Semi/Anti 投影语义，以及使用 Associativity、Left Asscom 还是 Right Asscom。

```text
语义规则生成合法候选
        ↓
统计与 Cost 比较候选
        ↓
选择最低代价 Join Tree
```

Cost 再低也不能让错误重排变得正确。

### Calcite 传统路径：MultiJoin 与 Lopt

[`JoinToMultiJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToMultiJoinRule.java) 把 Join Tree 压平成 `MultiJoin`，但保留 Outer Join 语义：

- Null-Generating Side 的子树不会被无条件拉平；
- 每个输入保留 Join Type 和 Outer Join Condition；
- Post-Join Filter、Projection Field 和字段引用计数被记录。

[`LoptOptimizeJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/LoptOptimizeJoinRule.java) 再根据 Metadata 构造顺序。它还能在 Null-Generating Factor 的 Join Key 唯一且没有列被投影时删除无用 Outer Join，并选择可能降低扫描成本的 Semi Join。

[`Programs.heuristicJoinOrder()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java) 展示了分阶段用法：Join 数量超过阈值后，先用 Hep 生成 MultiJoin，再移除会导致穷举爆炸的 Commute/Associate Rule，最后使用 Lopt 展开。

### Calcite 新路径：HyperGraph、Conflict Rule 与 DPHyp

当前快照还包含一套 Experimental HyperGraph 路径：

```text
Join Tree
  → JoinToHyperGraphRule
  → HyperGraph：Node + HyperEdge + ConflictRule
  → DphypJoinReorderRule
  → DpHyp 枚举 CSG-CMP
  → Metadata Cost 选出最佳 Join Tree
```

[`JoinToHyperGraphRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java) 支持 INNER、LEFT、FULL、SEMI 和 ANTI，把 Join Condition 转成 HyperEdge。它用 `long` 表示节点 Bitmap，因此最多处理 64 个输入。

[`ConflictDetectionHelper`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/ConflictDetectionHelper.java) 根据论文 [On the Correct and Complete Enumeration of the Core Search Space](https://dl.acm.org/doi/10.1145/2463676.2465314) 的 CD-C 算法，为不同 Join Type 编码 Associativity、Left Asscom 和 Right Asscom 约束。

[`DpHyp`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DpHyp.java) 只枚举由 HyperEdge 连接且满足冲突约束的 Connected Subgraph/Complement Pair。[`DphypJoinReorderRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DphypJoinReorderRule.java) 默认用 `bloat=127` 限制 DP Table 增长。

Core 中存在这些 Rule，不等于 Standard Program 会自动启用它们。应用仍要选择规则、阶段、阈值和 Cost Model。

### StarRocks：小图穷举、大图启发式

StarRocks [`QueryOptimizer.memoOptimize()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) 根据 Join 数量选择策略：

- 小规模 Inner/Cross Join 向 Memo 加入 Commute、Associate、Left Asscom；
- 超过 Exhaustive 阈值后调用 `ReorderJoinRule`，使用 Left-Deep、DP 或 Greedy；
- 超过最大阈值时停止 Reorder，避免规划失控；
- Outer Join Reorder 由 Session 开关和 `capableOuterReorder()` 约束。

[`JoinReorderProperty`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java) 用矩阵记录 Join Type 的结合与 Asscom 能力。源码明确指出尚无通用 Null-Rejecting Evaluation，因此标记为 `CONDITIONAL_SUPPORTED` 的组合不执行。

这形成两层策略：先由 `convertOuterToInner()` 消除可收紧的 Outer Join；对剩余 Outer Join，只生成矩阵明确安全的候选。

### Doris：局部 Exploration 与 DPHyp 并存

Doris Nereids 同时具备局部 Rule 和 HyperGraph：

- [`OuterJoinLAsscomProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinLAsscomProject.java) 列出允许的 `(Bottom Join, Top Join)` 组合；
- [`OuterJoinAssocProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinAssocProject.java) 在特定 Left-Left 组合上检查 Null-Rejecting；
- [`ConflictRulesMaker`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java) 把不满足结合律的关系编码为 HyperEdge 依赖；
- [`JoinOrderJob`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/JoinOrderJob.java) 使用 DPHyp；图过大时由 `GraphSimplifier` 简化。

支持 Outer Join 不代表所有形态都能任意重排，Projection、Hint、谓词范围和 Null-Rejecting 条件仍会缩小空间。

## CBO 之后为什么还需要物理计划优化

### 三类阶段不能混为一谈

| 阶段 | 输入 | 典型工作 | 是否需要全局重新计价 |
|------|------|----------|----------------------|
| 逻辑规范化 | Logical Tree | Subquery、常量、谓词、Outer Join 消除 | 通常不需要 |
| CBO/Memo 搜索 | 等价 Group + Trait | Join Order、Join 实现、Distribution、Aggregate Phase | 需要 |
| 物理后处理 | 已选 Physical Tree | Runtime Filter、局部节点合并、表达式复用、Fragment 信息 | 原则上不应改变全局决策 |

Hash Join 的 Build/Probe、Exchange、Distribution 和 Fragment 边界只有在最佳计划提取后才完整。Runtime Filter、Dictionary、Late Materialization 和局部 Common Subexpression 面向唯一物理树执行即可；为 Memo 中每个候选都构建它们成本过高。

### Calcite：Program Sequence 与第二次 Calc Pass

[`Programs.standard()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java) 的默认阶段是：

```text
SubQuery
  → Decorrelate
  → Measure Rewrite
  → Trim Fields
  → 主 Planner.findBestExp()
  → 第二次 Hep Calc Pass
```

源码把最后一次 `calc(metadataProvider)` 称为物理 “tweaks”。`Programs.sequence()` 允许应用继续组合自己的 Program。稳妥原则是：CBO 后处理应保持输出 Trait 和全局结构的代价假设；如果重新选择 Join Order、Build Side 或 Distribution，就应重新进入 Cost Search。

### StarRocks：PhysicalRewrite 与 DynamicRewrite

StarRocks 从 Memo 提取计划后执行 [`physicalRuleRewrite()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java)：

```text
PreAggregate
  → Exchange/Sort 与无效 Shuffle 裁剪
  → Distribution/Aggregate 局部优化
  → 低基数字典与 Decode
  → MinMax Statistic
  → Predicate Reorder
  → Join Local Shuffle
  → Index-only Predicate
  → Data Cache
  → Global Late Materialization
```

随后 `dynamicRewrite()` 应用 Skew Shuffle Join Elimination 和历史 Tuning Guide。源码特意在 Physical Rewrite 前记录 Cost，因为后处理后的 Statistics 不再保证与原 CBO 阶段一致。

### Doris：PlanPostProcessors

Doris [`NereidsPlanner`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java) 的主线是：

```text
Analyze → RBO Rewrite → Pre-MV Rewrite → Optimizer/Memo
  → chooseBestPlan → PlanPostProcessors → PhysicalPlanTranslator
```

[`PlanPostProcessors`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java) 处理 Filter/Project、Partition Predicate、Shuffle Key、Common Subexpression、TopN、Fragment、Runtime Filter 和 Validator。

Runtime Filter 位于 Fragment Processor 之后，因为它需要最终 Join 方向、表达式映射和 Scan Target；Pruner 还要删除收益不足或不能到达 Scan 的过滤器。

## 运行时统计与 Dynamic Filter：Calcite 的边界

### 先区分四种机制

| 机制 | 信息产生时间 | 影响范围 | 是否改变当前查询物理计划 |
|------|--------------|----------|--------------------------|
| Static Statistics | 规划前 | 当前查询 CBO | 是，规划期选择 |
| History-Based Optimization | 历史执行后 | 后续同类查询 | 是，下一次规划 |
| Runtime/Dynamic Filter | 当前 Build Side 运行后 | 当前查询尚未扫描的数据 | 通常不改 Join Tree |
| Mid-query Re-optimization | 当前查询执行中 | 当前查询剩余 Pipeline | 是，需要状态迁移 |

Runtime Filter 通常不重新选择 Join Order，而是把 Build Side 键集合摘要送到 Probe Scan。

### Calcite Core：能接入反馈，但不拥有执行闭环

在本文快照的 `core/src/main/java` 中，没有统一的分布式 Runtime Filter/Dynamic Filter RelNode、Build/Merge/Wait 协议或 Mid-query Re-optimization 生命周期。这与 Calcite 定位一致：Core 不拥有 Fragment、Exchange Channel、Scan Reader 和运行状态。

Calcite 提供的支点包括：

- 自定义 `RelMetadataProvider` 注入外部或历史统计；
- 通过 `Context` 向 Rule 和 Cost Model 传递信息；
- 自定义物理 RelNode 和 Rule 承载 Dynamic Filter 描述；
- 用 `Programs.sequence()` 或重新调用 Planner 组织阶段。

而 Build 键收集、IN/MinMax/Bloom 选择、分区合并、跨节点传输、Probe 等待、Scan 下推和 Profile 必须由引擎实现。

所以准确答案是：**Calcite 可以承载 Dynamic Filter 的规划表达，但 Calcite Core 没有完整执行实现。**

### StarRocks：Runtime Filter 与跨查询 Tuning Guide

StarRocks 在物理 PlanNode 阶段创建 [`RuntimeFilterDescription`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterDescription.java)，再通过 `PlanNode.pushDownRuntimeFilters()` 穿过 Project、Exchange 等节点寻找 Probe Target。BE 负责分区过滤器合并、传输和 Scan 下推。这是当前查询的信息旁路，不是重新运行 Memo。

另一条跨查询链路是 [`PlanTuningAdvisor`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/feedback/PlanTuningAdvisor.java)：它缓存 Profile 分析得到的 `OperatorTuningGuides`，后续相似查询由 `ApplyTuningGuideRule` 在 Dynamic Rewrite 阶段应用。这属于 History-based Tuning，而不是 Mid-query Replan。

### Doris：HBO 与 Runtime Filter Post Process

Doris Nereids 具有 HBO 基础设施：

- [`HboPlanStatisticsProvider`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboPlanStatisticsProvider.java) 保存历史 Plan Statistics；
- [`HboStatsCalculator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboStatsCalculator.java) 匹配历史信息；
- Cost Model 可参考历史 Row Count、Runtime Filter 安全性和倾斜；
- Session Variable 分别控制信息采集、HBO 优化和宽松匹配。

这仍是历史执行反馈给下一次规划。

当前查询的 Runtime Filter 由 [`RuntimeFilterGenerator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java) 在物理后处理阶段生成：

1. Bottom-Up 遍历 Physical Join；
2. 从 Hash Conjunct 生成允许的 IN、MinMax、Bloom；
3. PushDown Visitor 穿过 Project/Join 解析 Scan Target；
4. `RuntimeFilterPruner` 删除无效过滤器；
5. `RuntimeFilterTranslator` 绑定执行层 Fragment/Scan。

这清晰划分了职责：CBO 决定 Join Tree 和 Build/Probe，Post Process 构造运行时旁路，执行层负责数据结构与等待协议。

## 三套优化器的横向对比

| 维度 | Apache Calcite | StarRocks Optimizer | Doris Nereids |
|------|----------------|---------------------|---------------|
| 定位 | 可嵌入优化框架 | 一体化查询优化器 | 一体化查询优化器 |
| 逻辑/表达式 | `RelNode` / `RexNode` | `Operator` / `ScalarOperator` | `Plan` / `Expression` |
| 启发式改写 | Hep + Program | Tree Rewrite Task + RuleSet | Rewriter + Rewrite Job |
| 代价搜索 | Volcano `RelSet/RelSubset` | Memo `Group/GroupExpression` | Memo `Group/GroupExpression` |
| 物理属性 | 可扩展 `RelTraitSet` | `PhysicalPropertySet` | `PhysicalProperties` |
| 元数据 | 可插拔 `RelMetadataQuery` | 内置 Statistics/Property Deriver | 内置 Stats/DataTrait |
| Null-Rejecting | `Strong` + Metadata | `canEliminateNull` + Join Pushdown | Slot→NULL 后常量折叠 |
| Outer Join Reorder | MultiJoin/Lopt；Experimental DPHyp | CD-C 矩阵 + Memo/启发式 | Outer Join Rule + DPHyp |
| CBO 后处理 | Program Sequence | PhysicalRewrite + DynamicRewrite | PlanPostProcessors |
| 历史反馈 | 由应用注入 | Plan Tuning Advisor | HBO Provider/Calculator |
| Runtime Filter | Core 无执行闭环 | FE 规划 + BE Build/Merge/Scan | Nereids Post Process + BE |

### Calcite 的抽象是不是冗余

对于固定执行引擎，Calcite 确实有额外成本：概念多、Metadata 调用隐蔽、通用 Rule 偏保守、版本升级会影响扩展点，而且执行能力无法仅靠 Core 获得。

但这些抽象让多数据源共享 Parser、Validator 和逻辑规则，让 Adapter 定义自己的 Convention 与 Pushdown，也让 Metadata、Rule、Cost 和物理实现可以独立替换。

因此更准确的评价是：**Calcite 用框架复杂度换生态复用；自研优化器用重复建设成本换垂直整合和演进控制。**

### 真正决定质量的不是 Memo 类名

```text
语义覆盖：NULL、Outer/Semi/Anti、相关子查询是否正确
搜索空间：是否生成关键 Join Order、Distribution、Aggregate Phase
统计质量：NDV、MCV、Histogram、Correlation、历史反馈是否可靠
代价模型：网络、并发、内存、Spill、缓存是否匹配执行器
搜索预算：复杂查询能否在时限内保留高价值候选
执行闭环：Runtime Filter、Profile、HBO 是否反馈到后续决策
```

Memo 只是保存和复用搜索状态的数据结构，不会自动解决这些问题。

## 如何设计和验证优化器

### 推荐阶段边界

```text
Analyze / Type / Nullability
  → Subquery 与 Apply 消除
  → Expression Normalize / Constant Fold
  → Predicate Infer / Pushdown
  → Outer Join Simplification
  → Column / Partition Prune
  → Memo Exploration + Implementation + Enforcer + Cost
  → Extract Best Physical Plan
  → Runtime Filter / Fragment / Local Physical Rewrite
  → Validate
  → Execute / Profile / History Feedback
```

两条原则：

1. **能扩大搜索空间的语义改写尽量进入 CBO 之前。** Outer→Inner 会解锁更多 Join Reorder。
2. **CBO 后只做局部、可验证、不推翻全局代价选择的改写。** 改变 Distribution 或 Build Side 时应重新计价。

### Rule 测试不能只验证结果行

| 测试类型 | 需要验证的内容 |
|----------|----------------|
| Positive | 应触发的 SQL 是否生成预期结构 |
| Negative | Outer/Anti/Null-safe 危险语义是否保持 |
| Fixpoint | 多规则组合是否收敛、是否重复生成等价节点 |
| Property | Row Type、Nullability、Trait、Output Slot 是否一致 |

Null-Rejecting 和 Outer Join Reorder 应覆盖：

```sql
b.x > 1
b.x IS NULL
coalesce(b.x, 0) > 1
a.x IS NOT DISTINCT FROM b.x
NOT EXISTS (...)
NOT IN (...)
LEFT / RIGHT / FULL OUTER JOIN
```

还应使用包含 NULL、重复键和空 Build Side 的数据集验证等价性。

### CBO 调试的证据链

1. **候选是否存在？** Rule 未生成候选时，调 Cost 没有意义。
2. **Trait 是否可达？** 缺少 Converter/Enforcer 时，候选无法满足 Root Property。
3. **基数从哪里错？** 检查 Scan、Filter、Join 每层 RowCount。
4. **Cost 哪一项主导？** CPU、Memory、Network、Spill 权重是否合理？
5. **是否被预算截断？** Join 数、Rule 次数、DP Table、Timeout 是否提前终止？
6. **后处理是否改坏计划？** 比较 Memo Best Plan 与最终 Fragment。
7. **Runtime Filter 是否到达 Scan？** 生成 Filter 不等于有有效 Target，也不等于及时到达。

### Unknown 不是 Zero，超时也不是最优

优化器工程中还有两个容易被日志掩盖的事实。

第一，Metadata Provider 返回未知值，不应被解释成零行、零成本或“没有网络”。未知意味着当前证据不足，系统应使用保守默认值、区间或置信度，并把 fallback 原因暴露出来。否则缺统计的计划反而会因为虚假的低成本获胜。

第二，Volcano/Memo 保存了更大的候选空间，不等于有限时间内一定得到全局最优计划。Rule Queue、Match Limit、Join Enumeration Threshold、Planner Timeout 和 Cost Pruning 都会改变实际搜索边界。线上计划应记录：候选数、Rule 命中/产出数、被剪枝原因、Metadata fallback 次数、优化耗时，以及是否因预算提前结束。

Calcite 已提供 [`RuleMatchVisualizer`](https://calcite.apache.org/javadocAggregate/org/apache/calcite/plan/visualizer/RuleMatchVisualizer.html) 和 `RelOptListener` 事件来观察 Rule Attempt、Production、Equivalence 与 Chosen Plan。对复杂 Rule 集而言，可视化“候选如何产生和消失”通常比只看最终 `EXPLAIN` 更快定位问题：最终计划差，可能是 Cost 选错，也可能是正确候选从未生成。

## 总结

Calcite 最值得学习的不是某一条 Rule，而是清晰的组合边界：

```text
RelNode/RexNode 表达语义
  → Trait 表达物理要求
  → Metadata 提供可推导事实
  → Hep 编排确定性改写
  → Volcano 保存候选并按 Cost 搜索
  → Program 组织多阶段优化
  → Adapter/Engine 接管物理执行
```

围绕最初的五个问题，可以得到明确答案：

1. **Null Constraint 推导**依赖 Null-Rejecting、Join Type 和传播方向；Semi/Anti 不能共用简单规则。
2. **Left Join 可以重排，但只能在语义约束允许的空间内重排。** MultiJoin 或 HyperGraph 必须保留 Null-Generating Side 与冲突依赖。
3. **Hep 负责可控收敛，Volcano/Memo 负责保留候选和全局计价。**
4. **CBO 后仍需要物理优化，** 因为 Runtime Filter、Fragment 和存储能力依赖最终拓扑；但后处理不能无成本地推翻全局选择。
5. **运行时统计与 Dynamic Filter 不属于 Calcite Core 的完整职责。** Calcite 提供承载接口，StarRocks、Doris 通过 FE/BE 闭环完成执行。

如果只看代码直接性，StarRocks 和 Doris 更容易把优化决策落实到 Runtime Filter、Pipeline 与 Storage；如果需要支持多种 Convention 和数据源，Calcite 的抽象提供了更稳定的扩展边界。

最终的工程选择不是“通用框架一定优于自研”，而是：

- 需要复用多少 SQL 与关系代数基础设施？
- 有多少优化必须深入执行器和存储层？
- 团队是否愿意长期维护 Rule、Statistics、Cost Model 与调试工具？

## 关键源码阅读索引

| 主题 | 项目 | 源码入口 |
|------|------|----------|
| Hep Planner | Calcite | [`HepPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) |
| Hep Program | Calcite | [`HepProgramBuilder.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java) |
| Volcano Planner | Calcite | [`VolcanoPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) |
| Predicate Metadata | Calcite | [`RelMdPredicates.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java) |
| Null-Rejecting | Calcite | [`Strong.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java) |
| Outer Join Simplification | Calcite | [`FilterJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java) |
| HyperGraph Join Reorder | Calcite | [`JoinToHyperGraphRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java) |
| CBO 与物理后处理 | StarRocks | [`QueryOptimizer.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) |
| Predicate 推导与 Join 收紧 | StarRocks | [`JoinPredicatePushdown.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java) |
| Outer Join Reorder 属性 | StarRocks | [`JoinReorderProperty.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java) |
| Nereids 主流程 | Doris | [`NereidsPlanner.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java) |
| Outer Join 消除 | Doris | [`EliminateOuterJoin.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java) |
| HyperGraph 冲突规则 | Doris | [`ConflictRulesMaker.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java) |
| 物理计划后处理 | Doris | [`PlanPostProcessors.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java) |
| Runtime Filter 生成 | Doris | [`RuntimeFilterGenerator.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java) |

---
title: "【源码】深入 Calcite 优化器：Hep、Volcano、谓词推导与 Join Reorder"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从启发式原地重写与等价类代价搜索的根本张力出发，结合 Apache Calcite、StarRocks 与 Apache Doris 源码剖析 Hep/Volcano 双引擎架构、Null-Rejecting 推导、Outer Join 重排以及优化器与执行器的真实职责边界。"
draft: false
toc: true
math: true
---

> 本系列第 1/4 篇 · 优化器 / Cascades · [下一篇]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}})

初学查询优化器的工程师在设计第一版内核时，几乎都会撞上一道非此即彼的两难选择题：

- **如果只写一个原地替换树的规则引擎（每匹配一条规则就把旧子树覆盖成新子树）会怎样？** 面对子查询解关联、常量折叠、谓词下推这类“做了大概率稳赚不赔”的化简，它跑得飞快；可一旦遇到多表连接顺序（Join Order）或分布式数据洗牌（Shuffle vs Broadcast），把 `A ⋈ B` 原地改成 `B ⋈ A` 就会把旧树彻底抹掉——万一上层紧跟着 `GROUP BY A.k`，刚才被抹掉的旧树恰好能省掉一次全网重洗牌呢？
- **那干脆反过来，把刚解析完的原始 SQL 关系代数树直接扔进保留所有历史候选的等价类图（Memo）里全量穷举，行不行？** 灾难来得更快！假设 SQL 写了 `a LEFT JOIN b ON a.id = b.id WHERE b.score > 10`，如果不在进搜索空间前先利用 `b.score > 10` 把 `LEFT JOIN` 化简成 `INNER JOIN`，连接交换律与结合律根本推不动；而如果把几十条表达式归一化、投影裁剪与连接重排规则一锅粥全塞进 Memo，等价类数量会在几毫秒内指数级爆炸。

正是这道绕不开的物理张力，塑造了现代生产级查询优化器几乎一致的骨架：**先用可编排的启发式重写（RBO）做原地规范化、打薄输入树并解锁代数对称性，再送进按“语义等价类 + 物理属性（Trait）”分桶的动态规划空间（CBO）比拼全局代价，最后在选定的唯一物理树上做局部执行适配。**

作为大数据生态中最具代表性的可嵌入优化器框架，Apache Calcite（支撑着 Flink、Hive、Druid 等众多系统）把这套骨架抽象成了双引擎——[`HepPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) 与 [`VolcanoPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java)。然而围绕 Calcite 常有两个截然相反的误解：有人以为“接上 Calcite 就白得了一整套成熟的分布式 CBO 与 Runtime Filter”；也有人嫌它的 `RelTraitSet`、`RelMetadataQuery` 层层套娃过于繁琐。

要看清哪些复杂度是“通用框架为适配多引擎必须付的税”，哪些又是“任何严肃优化器都逃不掉的硬骨头”，最好的办法就是把 **Apache Calcite** 与两套垂直整合的自研 MPP 优化器——**StarRocks Optimizer** 和 **Apache Doris Nereids**——摆在同一个解剖台上，顺着一条查询从逻辑化简、外连接重排到运行时反馈的真实闯关路径逐层比对。

---

## 一、双引擎骨架：Hep 原地归约、Volcano 等价账本与 Metadata 总线

在 Calcite 里，一条 SQL 经历 `SqlNode`（语法解析）和 `Validator`（名称绑定与类型校验）后，落地为关系算子树 `RelNode` 与标量表达式 `RexNode`。为了不把具体的存储引擎或算子实现写死在框架里，Calcite 把优化所需的全部要素拆成了四组正交抽象：

| 核心抽象 | Calcite 载体 | 承载的工程契约 |
| --- | --- | --- |
| **关系结构** | `RelNode` | 描述 `TableScan`、`Filter`、`Project`、`Join`、`Aggregate` 等代数拓扑 |
| **标量与三值逻辑** | `RexNode` | 表达列引用（`RexInputRef`）、常量、函数调用及包含 `UNKNOWN` 的 SQL 三值逻辑 |
| **物理属性契约** | `RelTraitSet` | 刻画执行引擎方言（`Convention`）、数据分布（`Distribution`）与排序方向（`Collation`） |
| **事实与统计总线** | `RelMetadataQuery` | 按需推导行数、选择率、唯一键、上提谓词（Pulled-up Predicates）与累积代价 |

有了这四块积木，`HepPlanner` 与 `VolcanoPlanner` 的协同分工就水落石出了：

![Calcite Hep 确定性重写与 Volcano/Memo 等价空间搜索协同](/images/diagrams/aug-opt-calcite-hep-vs-volcano.svg)

*图 1：Calcite 双引擎流水线——先由 `HepPlanner` 在单根可变 DAG 上按确定次序完成解关联、谓词下推与外连接化简，再交由 `VolcanoPlanner` 在 `RelSet / RelSubset` 两层账本中结合 `RelMetadataQuery` 搜索全局最低成本物理树。*

### 1. `HepPlanner`：把控执行次序的原地手术刀

[`HepPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) 把当前计划维护为一棵单根有向无环图（DAG，利用表达式 Digest 合并公共子结构），严格遵照 [`HepProgramBuilder`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java) 编排的指令剧本推进：你可以精确指定先按 `Depth-First` 还是 `Bottom-Up` 跑哪一组规则、最多匹配多少轮，或者一直迭代到不再产生新变化的不动点（Fixpoint；新近主干还将图顶点存活校验前置到遍历循环并增加了 `matches()` 快速预检以削减空转开销）。

为什么规则的**先后次序**如此重要？拿外连接与连接重排举例：如果先做连接重排，树里躺着一堆 `LEFT JOIN`，重排规则束手无策；唯有先跑常量折叠与谓词推导，识别出拒绝空值的过滤条件，接着把 `LEFT JOIN` 降级为 `INNER JOIN`，最后再交给重排阶段，搜索空间才能彻底打开。`HepPlanner` 每次命中规则都会原地替换节点、不留历史包袱，恰好以极低内存开销胜任这类确定性前置归约。

### 2. `VolcanoPlanner`：两层等价集合与物理 Trait 竞标

一旦进入“局部占优不等于全局最优”的深水区，就必须轮到 [`VolcanoPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) 登场（其底层如何传播成本与协商 Trait，我们将在[下一篇专题]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}})中深入源码拆解）。它不再覆盖旧树，而是维护两级容器（同时支持通过 `EmptyRowTypePolicy` 拦截异常零列关系节点）：

- **`RelSet`（逻辑等价组）**：凡是关系语义完全相同的表达式（如 `A ⋈ B` 与 `B ⋈ A`），统统归入同一个 `RelSet`；
- **[`RelSubset`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)（物理属性分桶）**：在同一个 `RelSet` 内部，再按 `RelTraitSet`（例如 `[Convention=ENUMERABLE, Distribution=HASH(k)]` vs `[Convention=ENUMERABLE, Distribution=SINGLETON]`）切分成若干子集，每个 `RelSubset` 独立打擂台，只记住满足该物理契约的最低代价胜出者。

这与 StarRocks 和 Doris Nereids 中的 `Memo → Group → GroupExpression + lowestCostPlans[PhysicalProperties]` 在数学上完全同构。唯一的架构分野在于统计与属性是如何喂给规则的：StarRocks 与 Doris 把统计推导器（`StatisticsCalculator`）与属性推导器直接内聚在自家算子体系里；而 Calcite 为了让 Flink、Hive 各自挂载不同的统计源，抽出了动态分派总线 [`RelMetadataQuery`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMetadataQuery.java)——规则本身不存状态，每当需要知道行数、唯一键或下层谓词时，就向 `RelMetadataQuery` 发问，由底层字节码生成的 Handler 沿着 `RelNode` 树推导并缓存。这层间接性换来了无与伦比的跨引擎扩展力，代价则是阅读源码时必须同时追踪规则与 Metadata Handler 两条调用链。

---

## 二、撬开搜索空间的第一把钥匙：三值逻辑与 Null-Rejecting 推导

理解了搜索引擎，我们马上来看实战中最能拉开计划质量差距的第一关：**如何把束手束脚的 `OUTER JOIN` 安全化简为自由度极高的 `INNER JOIN`？**

看一条日常业务报表里再普通不过的查询：

```sql
SELECT a.id, b.score
FROM a
LEFT JOIN b ON a.id = b.id
WHERE b.score > 10;
```

左外连接的语义是：当左表 `a` 的某行在右表 `b` 中找不到配对时，在右侧所有列补上 `NULL` 后照常输出。可紧接着上层的 `WHERE` 子句要求 `b.score > 10`——按照 SQL 三值逻辑（`TRUE` / `FALSE` / `UNKNOWN`），把补出来的 `b.score = NULL` 代入 `NULL > 10`，求值结果是 `UNKNOWN`；而 `WHERE` 过滤算子只放行严格为 `TRUE` 的行！这意味着那些辛苦补出来的 `NULL` 行在跨过 `Filter` 时全军覆没，一条也活不下来。既然如此，这层 `LEFT JOIN` 从头到尾就等价于 `INNER JOIN`！

要让优化器自动完成这次推理，核心数学命题只有一个：**当把某侧补空列集合全部代换为 `NULL` 时，目标谓词求值结果是否绝对不可能为 `TRUE`（必然落在 `{FALSE, UNKNOWN}` 中）？** 满足这一性质的谓词，就叫做 **Null-Rejecting（拒绝空值谓词）**。

![Null-Rejecting 三值逻辑推导与 Outer Join 合法重排空间](/images/diagrams/aug-opt-calcite-null-join-reorder.svg)

*图 2：从三值逻辑 Null-Rejecting 证明到合法重排空间的完整决策链——能拒绝补空行的外连接直接降级为内连接解锁全空间交换；必须保留补空语义的外连接则交由 CD-C 超图冲突规则约束枚举，同时严防 Anti Join 与 `<=>` 误删合法空值。*

### 1. 三套引擎实现 Null-Rejecting 的精妙手法对比

围绕同一个数学命题，Calcite、StarRocks 与 Doris 写出了三种风格迥异的内核实现：

- **Calcite（基于算子策略类的静态归纳）**：[`Strong.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java) 为每种 `SqlKind` 标注了空值传播策略（例如 `Policy.ANY` 表示任一输入为空则结果必为空，如 `+`、`=`、`>`；而 `IS NULL`、`COALESCE`、`IS NOT DISTINCT FROM` 则走定制分支）。调用 `Strong.isNotTrue(expr, nullColumns)` 即可判定给定列集全为空时表达式是否必不为真。在此基础上，[`FilterJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java)（Smart 模式配合 [`RelOptUtil.simplifyJoin()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelOptUtil.java)）先一步将 `LEFT / RIGHT / FULL JOIN` 收紧降级，随后 [`RelMdPredicates`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java) 与 [`JoinPushTransitivePredicatesRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinPushTransitivePredicatesRule.java) 沿着连接等值条件把一侧谓词跨侧传染给另一侧（新近主干 `68f1613` 还专门修复了 `RelMdPredicates` 中 `RIGHT JOIN` 推导谓词曾错塞进左表列表的归类缺陷）。
- **StarRocks（一站式闭环下推）**：不走独立的元数据总线，直接在重写器 [`JoinPredicatePushdown.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java) 里一气呵成——`convertOuterToInner()` 利用 `Utils.canEliminateNull()` 将双侧均拒空的 `Full Outer` 直降 `Inner`、单侧拒空的分别收紧为 `Left` 或 `Right Outer`，紧接着调用 `rangePredicateDerive()`、`equivalenceDerive()` 与 `deriveIsNotNullPredicate()` 把派生出的非空与范围谓词存入 `OptimizerContext` 供整棵树共享。
- **Doris Nereids（直接代换 `NULL` 字面量做常量折叠）**：[`ExpressionUtils.inferNotNullSlots()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/util/ExpressionUtils.java) 走了一条极其务实的捷径——无需为几百个内置函数单独维护 `Strong` 策略表，直接把表达式里的目标 `Slot` 替换成同类型的 `NULL Literal`，扔给现成的常量折叠器 `FoldConstantRule.evaluate()` 算一把！只要折叠结果是 `NULL` 或 `FALSE`，该列就是铁板钉钉的拒空列（为防超宽表达式组合爆炸，代码对表达式宽度、深度与槽位数设了硬上限）。随后 [`InferFilterNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferFilterNotNull.java)、[`InferJoinNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferJoinNotNull.java) 与 [`EliminateOuterJoin`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java) 在不动点循环中层层剥笋，把嵌套外连接连环降级。

这里藏着两个极易翻车的语义暗礁，正好在最近的上游提交中留下了鲜活教材：

第一，**“过滤谓词拒空”与“聚合函数参数忽略空”绝不能共用同一个判据**。在 Doris `7ac0eb2f` 中，研发团队特意把原先统一的判据拆成了两套入口：`Filter` 谓词继续用“折叠结果为 `NULL` 或 `FALSE`”；而针对 `COUNT / SUM` 等忽略空值聚合函数推导非空输入时，新增的 `inferNotNullForNullIgnoringAggregate()` **只承认折叠结果为 `NULL`，绝不承认 `FALSE`**！为什么？因为 `FALSE` 在 `WHERE` 里确实留不下行，但 `COUNT(expr)` 里若 `expr` 求值为布尔值 `FALSE`，它是实打实要被计入总数的合法非空值，一旦当成拒空滤掉就会少算行数！

第二，**Semi / Anti Join 与多列空值安全比较的非对称陷阱**。在内连接 `a.k = b.k` 中，顺手给左右两边各补一个 `IS NOT NULL`（如 Calcite 的 [`JoinDeriveIsNotNullFilterRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinDeriveIsNotNullFilterRule.java)）不仅能裁剪扫描页，还能防止海量 `NULL` 键灌进同一个哈希分桶引发严重数据倾斜（Data Skew）。但到了 `LEFT ANTI JOIN`（`NOT EXISTS`），左表里 `a.k = NULL` 的行恰恰因为“跟右表谁都配不上”而成为**必须保留的合法未匹配输出**！因此 Calcite 严格限制 `JoinDeriveIsNotNullFilterRule` 只匹配 `JoinRelType.INNER`，且 `RelMdPredicates` 在 `ANTI` 分支严禁向上透传右表谓词。同样容易写错的还有复合行级空值比较——Calcite 在 `68f1613` 中刚刚修掉了 `RelOptUtil.isDistinctFrom()` 的一处陈年 Bug：旧代码展开多字段 `ROW(a, b) IS DISTINCT FROM ROW(c, d)` 时竟然无论正反都用 `AND` 拼接，导致“必须两个字段同时不同才算不同”，修正为 `neg ? AND : OR` 并递归展开嵌套 `struct` 后才恢复了正确的德摩根对偶语义。

---

## 三、带镣铐跳舞：Outer Join Reorder 的合法枚举算法

经过上一轮化简后，如果计划里依然留着无法消除的真·外连接，优化器还能调整连接顺序吗？

在内连接世界里，交换律 $A \bowtie B = B \bowtie A$ 与结合律 $(A \bowtie B) \bowtie C = A \bowtie (B \bowtie C)$ 畅通无阻；可一旦换成左外连接 $\mathbin{\text{⟕}}$，不仅交换律当场失效，连结合律与左交换结合律（Left-Asscom）都是有条件成立的。随便举个例子：

$$(A \mathbin{\text{⟕}}_{A.k = B.k} B) \bowtie_{B.k = C.k} C = A \bowtie (B \bowtie C)$$

上面这步因为内连接谓词引用并拒绝了 `B.k = NULL`，外连接其实已经被吞掉了；但如果是两个左外连接串联：

$$(A \mathbin{\text{⟕}}_{A.x = B.x} B) \mathbin{\text{⟕}}_{B.y = C.y} C \neq A \mathbin{\text{⟕}}_{A.x = B.x} (B \mathbin{\text{⟕}}_{B.y = C.y} C)$$

随便给一组单行数据就能戳破不等式：令 `A` 为空集、`B = {(1, 2)}`、`C = {(2)}`——左边先做 `A ⟕ B` 得到空集，再 `⟕ C` 还是**空集**；右边先做 `B ⟕ C` 吐出 `(1, 2, 2)`，虽然在这个空 `A` 上仍为空，可一旦令 `A = {(1)}`、`B` 为空集、`C = {(NULL)}` 且外层条件含空值敏感逻辑（或普通等值下看 `B` 未匹配时右侧何时补空），当 `A = {(1)}`、`B` 为空时左边吐出 `(1, NULL, NULL)`，若把内层换成反连接或非拒空谓词，两侧行数与补空列值立刻分道扬镳！正如论文 [《On the Correct and Complete Enumeration of the Core Search Space》](https://dl.acm.org/doi/10.1145/2463676.2465314)（Moerkotte 等，SIGMOD 2013）系统证明的那样，任何代价模型都只能在**语义合法的候选子集**里挑便宜货——代价算得再准，也救不回一棵语义不等价的错树。

在工程实现上，三套系统沿着两条技术代际给出了各自的答卷：

1. **扁平化启发式与受限变换矩阵（Calcite 传统路径 & StarRocks）**：
   - Calcite 经典的 [`JoinToMultiJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToMultiJoinRule.java) 在拍平连接树时，只拉平内连接骨干，对补空侧（Null-Generating Side）保持边界隔离并登记外连接条件，随后由 [`LoptOptimizeJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/LoptOptimizeJoinRule.java) 构造启发式顺序（若发现补空表连接键唯一且无任何输出列被引用，还能顺手整表裁掉！[`Programs.heuristicJoinOrder()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java) 在表数超阈值时就会切到这条防爆路径）。
   - StarRocks 在 [`JoinReorderProperty.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java) 中硬编码了一张二维连接类型能力矩阵（区分无条件支持、不支持与 `CONDITIONAL_SUPPORTED`），由于尚未在重排矩阵内嵌通用运行时拒空证明，对标记为 `CONDITIONAL_SUPPORTED` 的危险组合一律保守跳过，配合 `QueryOptimizer.memoOptimize()` 的表数分级熔断阀守住规划时延。
2. **基于超图（HyperGraph）与 CD-C 冲突规则的 `DPHyp` 完备枚举（Calcite 实验路径 & Doris Nereids）**：
   - 为了不靠零散规则拼凑而漏掉最优树形，Doris Nereids（[`ConflictRulesMaker.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java) + [`JoinOrderJob.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/JoinOrderJob.java)，大图辅以 `GraphSimplifier` 剪枝，小图与局部变换同时配备 [`OuterJoinLAsscomProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinLAsscomProject.java) 与 [`OuterJoinAssocProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinAssocProject.java)）以及 Calcite 的实验特性（[`JoinToHyperGraphRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java)，用 64 位 `long` 位图表示最多 64 个关系顶点 + [`ConflictDetectionHelper`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/ConflictDetectionHelper.java) + [`DpHyp`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DpHyp.java)，默认以 `bloat = 127` 限制 DP 表膨胀）双双落地了 **CD-C + DPHyp** 算法：把破坏结合律与交换结合律的顺序约束直接编码成超边（HyperEdge）上的前置位掩码依赖，`DpHyp` 在自底向上枚举连通子图与连通补图对（`CSG-CMP`）时，凡是不满足冲突位掩码的组合在生成前就被一刀切掉，做到既不漏掉合法核心空间、又绝不踩进非法重排雷区。

---

## 四、出了 Memo 还没完：物理计划后处理与 Calcite 的真实执行边界

当 `VolcanoPlanner.findBestExp()`（或 StarRocks `extractBestPlan`、Doris `chooseBestPlan`）从根等价组里捞出总成本最低的那棵物理算子树时，规划工作结束了吗？恰恰相反，工业界优化器在这之后全都排着一长串**物理后处理（Post-CBO Physical Rewrite）**流水线！

为什么不在 Memo 里一步到位算完所有物理细节？算一笔搜索复杂度账就明白了：像 **Runtime Filter 拓扑穿线、低基数全局字典延迟解码（Decode）、全局延迟物化（Global Late Materialization，简称 GLM）、局部公共子表达式抽取以及 Fragment 管道切分**，它们要么强依赖“谁是 Build、谁是 Probe、哪里插了 Shuffle Exchange”的最终定型拓扑，要么如果在 Memo 里给成千上万个半成品候选逐一挂载，会把规划耗时直接拖垮十倍以上。

因此三套系统都在 CBO 出口之后设立了专门的“精装修阶段”：

- **Calcite [`Programs.standard()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java)**：在 `findBestExp()` 之后紧跟第二轮 Hep `calc()` 通道做物理表达式合并与微调（源码称之为物理 `tweaks`）；
- **StarRocks [`QueryOptimizer`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java)**：依次跑 `physicalRuleRewrite()`（冗余 Shuffle 裁剪、流式 `SortAgg` 标记、低基数字典插入、谓词重排、`WidenProjectionNullableRule` 对齐槽位可空性、最后运行 `GlobalLateMaterializationRewriter` 插入 `FETCH` 算子）与 `dynamicRewrite()`（倾斜洗牌消除与历史调优指南应用）；
- **Doris [`PlanPostProcessors.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java)**：在 [`NereidsPlanner`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java) 选出最优树后，串行完成单调函数分区裁剪推导（`AddMonotonicFunctionPruningPredicates`）、公共子表达式消除、TopN 延迟物化、Fragment 切分以及 `RuntimeFilterGenerator` 穿线与无效剪枝。

这里必须恪守一条铁律：**CBO 后处理只能做不推翻全局代价契约的局部增效手术，并且必须严守执行器的物理类型契约**（例如 StarRocks 主干特意规定 `WidenProjectionNullableRule` 必须在全局延迟物化之前跑，因为 `FETCH` 算子直接从存储层往目标 Slot 填数据，倘若上层投影把列的可空性收窄了，延迟物化就会生成可空性不匹配的非法内存引用！）。

顺着物理后处理再往前迈一步，就触及了很多人最关心的边界问题：**运行时统计反馈与 Dynamic Filter，到底归不归 Calcite 管？**

首先要把经常被一锅乱炖的四类“统计与动态优化”按时间轴切开：

| 优化机制 | 事实产生时机 | 作用半径 | 是否重构当前查询的 Join 树？ |
| --- | --- | --- | --- |
| **静态统计信息（Static Stats）** | 查询规划前 | 当前查询 CBO 计价 | 是，编译期敲定物理树 |
| **历史执行反馈（HBO / Tuning Guide）** | 上一次查询跑完后 | **后续**同骨架相似查询 | 是，纠偏下一次规划的基数或算子选型 |
| **运行时过滤（Runtime / Dynamic Filter）** | 当前查询 Build 端跑完瞬间 | 当前查询 Probe 端未读数据 | **否**，保持原树不变，走旁路摘要裁剪 |
| **执行中途重优化（Mid-Query Re-opt / AQE）** | 当前 Shuffle / Stage 物化边界 | 当前查询尚未启动的上游阶段 | 是，需运行期中断并迁移中间状态 |

翻遍 Calcite Core 的 `core/src/main/java`，你找不到任何一个分布式 `RuntimeFilter` 执行协议、跨节点 Bloom 合并器或者执行中途挂起流水线的状态机——这绝不是 Calcite 的缺陷，而是**可嵌入规划库与宿主执行内核的天然楚河汉界**：

![Calcite 的职责边界：规划、执行与反馈各自由谁负责](/images/calcite-feedback-boundary.svg)

*图 3：Calcite Core 与宿主引擎的职责切分——Calcite 提供 `RelMetadataProvider` 统计注入点、`Context` 参数通道与自定义物理 `RelNode` 表达能力；而 Build 端键值收集、跨节点合并投递、Scan 页级下推以及 Profile 历史统计回流（如 StarRocks [`PlanTuningAdvisor`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/feedback/PlanTuningAdvisor.java)、Doris [`HboPlanStatisticsProvider`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboPlanStatisticsProvider.java) + [`HboStatsCalculator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboStatsCalculator.java)），必须由拥有网络交换与存储扫描器的宿主引擎闭环实现。*

---

## 五、三套优化器全景对照、排障证据链与源码索引

走完从代数接入到运行期反馈的全程，我们可以把 Apache Calcite、StarRocks Optimizer 与 Doris Nereids 的架构选型浓缩进一张全景对比表：

| 对比维度 | Apache Calcite | StarRocks Optimizer | Doris Nereids |
| --- | --- | --- | --- |
| **核心架构定位** | 多数据源可嵌入代数与搜索框架 | 垂直整合 MPP 原生优化器 | 垂直整合 MPP 原生优化器 |
| **算子与标量表示** | `RelNode` / `RexNode` | `Operator` / `ScalarOperator` | `Plan` / `Expression` |
| **启发式重写层（RBO）** | `HepPlanner` + `HepProgram` | 树重写任务栈 + 分阶段 `RuleSet` | `Rewriter` + `RewriteJob` |
| **代价搜索账本（CBO）** | `VolcanoPlanner`（`RelSet / RelSubset`） | `Memo`（`Group / GroupExpression`） | `Memo`（`Group / GroupExpression`） |
| **物理属性载体** | 开放扩展 `RelTraitSet` | 固定三元组 `PhysicalPropertySet` | `PhysicalProperties` |
| **统计与元数据传递** | 动态分派总线 `RelMetadataQuery` | 内置 `StatisticsCalculator` 与属性推导器 | 内置 `StatsCalculator` 与 `DataTrait` |
| **Null-Rejecting 证明** | `Strong` 算子策略表 + 谓词元数据 | `Utils.canEliminateNull` + `JoinPredicatePushdown` | 槽位代换 `NULL` 后常量折叠（区分谓词与聚合参数） |
| **Outer Join 重排** | `MultiJoin + Lopt`；实验级 CD-C `DPHyp` | 二维能力矩阵 + 按表数分级回退 | 局部探索规则 + CD-C `DPHyp` 超图枚举 |
| **CBO 物理后处理** | `Programs.sequence` + 二阶段 `Calc` | `physicalRuleRewrite` + `dynamicRewrite` | `PlanPostProcessors` |
| **跨查询历史反馈** | 开放 `RelMetadataProvider` 供外部注入 | `PlanTuningAdvisor` 调优指南缓存 | `HboPlanStatisticsProvider` 历史统计匹配 |

一句话总结框架与自研的取舍：**Calcite 用多层抽象税换取了跨生态多 Convention 复用的上限；StarRocks 与 Doris 则用自研建设成本换取了从优化器物理属性直通列存 `Segment` 与向量化算子的极致垂直协同。**

在实际研发与线上慢 SQL 调优中，无论面对哪一套 CBO，最忌讳的就是“一看到烂计划就去改代价公式系数”。请务必按以下七步证据链顺藤摸瓜，并且时刻牢记两条工程底线——**“统计未知（Unknown）绝不等于零成本（Zero）”**，**“搜索超时截断（Timeout）绝不等于证明了全局最优”**：

1. **看合法候选到底生成了没有？**（借助 Calcite [`RuleMatchVisualizer`](https://calcite.apache.org/javadocAggregate/org/apache/calcite/plan/visualizer/RuleMatchVisualizer.html) / `RelOptListener` 或引擎 `TRACE LOGS OPTIMIZER` 确认外连接是否成功消除、重排规则是否因表数超限被熔断）；
2. **看目标物理属性（Trait / Property）是否可达？**（是否缺了对应的 Converter 或 Enforcer 导致整条分支无法满足根节点要求）；
3. **抓基数估算第一次偏离数量级的算子**（从底表 `Scan` NDV 到 `Filter` 联合选择率再到 `Join` 输出行数逐层核对，检查是否因缺统计回退到了荒谬默认值）；
4. **拆解胜出与落选候选的 Cost 分项**（究竟是内存惩罚、网络洗牌还是 CPU 权重主导了翻盘）；
5. **检查搜索预算闸门**（DP 表 `bloat` 上限、规则匹配次数与超时阈值是否提前砍掉了好计划）；
6. **比对出 Memo 瞬间的物理树与最终 Fragment**（确认物理后处理有没有意外破坏分布或可空性契约）；
7. **用 Positive / Negative / Fixpoint / Property 四维单测锁死规则边界**（特别是覆盖 `b.x IS NULL`、`coalesce(b.x, 0) > 1`、`ROW(a,b) IS DISTINCT FROM ROW(c,d)` 与 `NOT IN / NOT EXISTS` 等反例数据）。

最后整理三套优化器最核心的源码入口供对照精读：

| 核心主题 | 所属项目 | 关键源码路径 |
| --- | --- | --- |
| Hep 与 Volcano 双引擎核心 | Apache Calcite | [`HepPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java)、[`HepProgramBuilder.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java)、[`VolcanoPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java)、[`RelSubset.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java) |
| Null-Rejecting 与谓词传递推导 | Apache Calcite | [`Strong.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java)、[`FilterJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java)、[`RelMdPredicates.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java) |
| MultiJoin 启发式与超图 DPHyp 重排 | Apache Calcite | [`JoinToMultiJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToMultiJoinRule.java)、[`JoinToHyperGraphRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java)、[`ConflictDetectionHelper.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/ConflictDetectionHelper.java)、[`DpHyp.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DpHyp.java) |
| 优化总控、外连接收紧与重排矩阵 | StarRocks | [`QueryOptimizer.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java)、[`JoinPredicatePushdown.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java)、[`JoinReorderProperty.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java) |
| Nereids 空值折叠、CD-C 超图与后处理 | Apache Doris | [`NereidsPlanner.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java)、[`EliminateOuterJoin.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java)、[`ConflictRulesMaker.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java)、[`PlanPostProcessors.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java) |

---

> **源码基线说明**：本文初稿基于 `apache/calcite` 提交 `e8e0dd5`（2026-05-04，介于 1.41.0 与 [1.42.0 正式版](https://calcite.apache.org/docs/history.html#1420--2026-05-31)之间）、`StarRocks/starrocks` 提交 `0fd27fd`（2026-03-30）及 `apache/doris` 提交 `5202d06`（2026-08-21），并于 2026-10 对照上游开发线快照 Calcite `68f1613`、StarRocks `f977b53`、Doris `7ac0eb2f` 完成全量复核。两快照间核心框架分工未变；上游关键修正与演进（Calcite 修复多字段 `RelOptUtil.isDistinctFrom()` 的 `OR/AND` 组合逻辑及 `RelMdPredicates` 右外连接推导归类、新增 `EmptyRowTypePolicy`；StarRocks 在 `GlobalLateMaterializationRewriter` 前置入 `WidenProjectionNullableRule`；Doris 将 `inferNotNull` 按过滤谓词与忽略空值聚合参数分拆、放宽 `OuterJoin*Project` 规范化匹配并在 `RuntimeFilterPruner` 保留分区/分桶裁剪过滤器）均已同步融入正文。

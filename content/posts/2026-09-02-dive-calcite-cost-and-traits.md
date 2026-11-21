---
title: "【源码】深入 Calcite 优化器全链路：Hep/Volcano 骨架、谓词与 Join 重排、Cost 账本与 Trait Enforcement"
date: 2026-09-02T00:00:00+08:00
lastmod: 2026-11-21T00:00:00+08:00
slug: "dive-calcite-cost-and-traits"
aliases:
  - "/2026/08/24/dive-calcite-optimizer/"
categories:
  - 数据库
tags:
  - Apache Calcite
  - Cascades Optimizer
  - 查询优化器
  - CBO
  - Join Reorder
  - Trait
  - Enforcer
  - StarRocks
  - Apache Doris
description: "从启发式原地重写与等价类代价搜索的根本张力出发，结合 Apache Calcite、StarRocks 与 Apache Doris 源码，全链路剖析 Hep/Volcano 双引擎骨架、Null-Rejecting 与 Outer Join 重排、RelSubset 代价账本与 VolcanoCost 单维裁判、两种 RuleDriver 调度分水岭、Trait Enforcement 协议、真实运行 Trace 以及优化器与执行器的职责边界。"
draft: false
toc: true
math: true
---

> 本系列第 1/3 篇 · 优化器 / Cascades · [下一篇]({{< relref "2026-09-01-dive-optd-cascades-optimizer.md" >}})

初学查询优化器的工程师在设计第一版内核时，几乎都会撞上一道非此即彼的两难选择题：

- **如果只写一个原地替换树的规则引擎（每匹配一条规则就把旧子树覆盖成新子树）会怎样？** 面对子查询解关联、常量折叠、谓词下推这类“做了大概率稳赚不赔”的化简，它跑得飞快；可一旦遇到多表连接顺序（Join Order）或分布式数据洗牌（Shuffle vs Broadcast），把 `A ⋈ B` 原地改成 `B ⋈ A` 就会把旧树彻底抹掉——万一上层紧跟着 `GROUP BY A.k`，刚才被抹掉的旧树恰好能省掉一次全网重洗牌呢？
- **那干脆反过来，把刚解析完的原始 SQL 关系代数树直接扔进保留所有历史候选的等价类图（Memo）里全量穷举，行不行？** 灾难来得更快。假设 SQL 写了 `a LEFT JOIN b ON a.id = b.id WHERE b.score > 10`，如果不在进搜索空间前先利用 `b.score > 10` 把 `LEFT JOIN` 化简成 `INNER JOIN`，连接交换律与结合律根本推不动；而如果把几十条表达式归一化、投影裁剪与连接重排规则一锅粥全塞进 Memo，等价类数量会在几毫秒内指数级爆炸。

正是这道绕不开的物理张力，塑造了现代生产级查询优化器几乎一致的骨架：**先用可编排的启发式重写（RBO）做原地规范化、打薄输入树并解锁代数对称性，再送进按“语义等价类 + 物理属性（Trait）”分桶的动态规划空间（CBO）比拼全局代价，最后在选定的唯一物理树上做局部执行适配。**

作为大数据生态中最具代表性的可嵌入优化器框架，Apache Calcite（支撑着 Flink、Hive、Druid 等众多系统）把这套骨架抽象成了双引擎——[`HepPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) 与 [`VolcanoPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java)。然而围绕 Calcite 常有两个截然相反的误解：有人以为“接上 Calcite 就白得了一整套成熟的分布式 CBO 与 Runtime Filter”；也有人嫌它的 `RelTraitSet`、`RelMetadataQuery` 层层套娃过于繁琐。更耐人寻味的是，当你先读过经典的 Volcano / Cascades 论文，再打开 `VolcanoPlanner.java` 单步调试一条最普通的 `Join + ORDER BY` 查询时，不出十分钟还会撞上三个颠覆教科书直觉的现象：物理属性 `TraitSet` 被直接焊进了 Memo 算子节点里；账本上留着剪枝字段，默认搜索引擎却一行都不看；`EXPLAIN` 打印得有鼻子有眼的三维代价 `{rows, cpu, io}`，底层裁判其实只看第一列！

要看清哪些复杂度是“通用框架为适配多引擎必须付的税”，哪些又是“任何严肃优化器都逃不掉的硬骨头”，最好的办法就是把 **Apache Calcite** 与两套垂直整合的自研 MPP 优化器——**StarRocks Optimizer** 和 **Apache Doris Nereids**——摆在同一个解剖台上，顺着一条查询从 `HepPlanner` 逻辑化简、外连接重排，一路钻进 `VolcanoPlanner` 的代价账本、两种 `RuleDriver` 调度栈、Trait 补偿博弈与真实运行 Trace，直到 CBO 出口外的物理后处理与执行边界。

---

## 一、双引擎骨架：Hep 原地归约、Metadata 总线与 Volcano 两层 Memo

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

### 1.1 `HepPlanner`：把控执行次序的原地手术刀

[`HepPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java) 把当前计划维护为一棵单根有向无环图（DAG，利用表达式 Digest 合并公共子结构），严格遵照 [`HepProgramBuilder`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java) 编排的指令剧本推进：你可以精确指定先按 `Depth-First` 还是 `Bottom-Up` 跑哪一组规则、最多匹配多少轮，或者一直迭代到不再产生新变化的不动点（Fixpoint；新近主干还将图顶点存活校验前置到遍历循环并增加了 `matches()` 快速预检以削减空转开销）。

为什么规则的**先后次序**如此重要？拿外连接与连接重排举例：如果先做连接重排，树里躺着一堆 `LEFT JOIN`，重排规则束手无策；唯有先跑常量折叠与谓词推导，识别出拒绝空值的过滤条件，接着把 `LEFT JOIN` 降级为 `INNER JOIN`，最后再交给重排阶段，搜索空间才能彻底打开。`HepPlanner` 每次命中规则都会原地替换节点、不留历史包袱，恰好以极低内存开销胜任这类确定性前置归约。

### 1.2 `VolcanoPlanner`：把 `TraitSet` 焊进 `RelSubset` 节点与 `RelMetadataQuery` 总线

一旦进入“局部占优不等于全局最优”的深水区，就轮到 [`VolcanoPlanner`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) 登场。教科书里的 Cascades Memo `Group` 只代表逻辑等价类，物理属性只是组内 Winner 哈希表的查找键；而在 Calcite 里，物理属性直接长成了图里的节点。

当调用方执行 `setRoot(rel)` 时，规划器会递归调用 `registerImpl(rel, null)` 把整棵逻辑计划树拆解登记（在 1.43 开发线中，入口处还新增了一道 `checkEmptyRowType(rel)` 门禁，当上下文配置为 `EmptyRowTypePolicy.FORBIDDEN` 且遇到零列关系节点时直接拒绝注册）：

- 首先计算表达式摘要 `RelDigest` 在 `mapDigestToRel` 中做结构去重；
- **`RelSet`（逻辑等价组）**：凡是关系语义完全相同的表达式（如 `A ⋈ B` 与 `B ⋈ A`），统统归入同一个 `RelSet`；
- **[`RelSubset`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)（物理属性分桶）**：在同一个 `RelSet` 内部，再按 `RelTraitSet`（例如执行约定 `Convention.NONE` vs `ENUMERABLE`、数据分布 `Distribution` 或排序方向 `RelCollation`）切分成若干子集。**`RelSubset = RelSet × RelTraitSet` 本身就是一个继承自 `RelNode` 的算子节点**，登记时算子原本指向具体子节点的输入边会被全部替换成指向子 `RelSubset`。

看明白这一步设计，就理解了 Calcite 与 StarRocks / Doris 的同构与分野。如果父算子 `MergeJoin` 要求左输入必须满足“`ENUMERABLE` 约定且按第 `0` 列升序”，它不需要维护额外的属性需求映射表——直接把左输入指向子 `RelSet` 里 trait 为 `ENUMERABLE.[0]` 的那个 `RelSubset` 实例即可，哪怕此刻这个桶里还是空的。在数学上，这与 StarRocks 和 Doris Nereids 中的 `Memo → Group → GroupExpression + lowestCostPlans[PhysicalProperties]` 完全等价；真正的架构差异在于**统计与属性如何喂给规则**：StarRocks 与 Doris 把统计推导器（`StatisticsCalculator`）和属性推导器内聚在自家算子体系里，而 Calcite 为了让 Flink、Hive 各自挂载不同的统计源，抽出了动态分派总线 [`RelMetadataQuery`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMetadataQuery.java)——规则本身不存状态，每当需要行数、唯一键或下层谓词时，就向 `RelMetadataQuery` 发问，由字节码生成的 Handler 沿 `RelNode` 树推导并缓存。这层间接性换来了极强的跨引擎扩展力，代价则是读源码时必须同时追踪规则与 Metadata Handler 两条调用链。

逻辑树入库之后，核心搜索入口 `findBestExp()` 的整个生命周期只有干净利落的五步：

```text
findBestExp()
  ├─ ① ensureRootConverters()        为与根 subset 仅差 1 个 trait 的同组 subset 挂接 AbstractConverter
  ├─ ② registerMaterializations()    将物化视图候选注入 Memo 等价类
  ├─ ③ ruleDriver.drive()            ★ 唯一的规则搜索主循环
  │      ├─ IterativeRuleDriver      默认路径：FIFO 队列穷举，无代价上界剪枝
  │      └─ TopDownRuleDriver        可选路径：Cascades 显式任务栈 + 分支限界剪枝
  ├─ ④ dumpRuleAttemptsInfo()        输出规则命中诊断统计
  └─ ⑤ root.buildCheapestPlan(this)  ★ 沿各 RelSubset 现成的 best 指针直接拼出物理树
```

这里先留心第 ① 步 `ensureRootConverters()` 里的一个经典源码陷阱：

```java
for (RelNode rel : root.getRels()) {
  if (rel instanceof AbstractConverter && !topDownOpt) {
    subsets.add((RelSubset) ((AbstractConverter) rel).getInput());
  }
}

for (RelSubset subset : root.set.subsets) {
  final ImmutableList<RelTrait> difference =
      root.getTraitSet().difference(subset.getTraitSet());
  if (difference.size() == 1 && subsets.add(subset)) {
    register(new AbstractConverter(subset.getCluster(), subset,
        difference.get(0).getTraitDef(), root.getTraitSet()), root);
  }
}
```

很多人以为开启自顶向下模式（`topDownOpt = true`）后，系统就会彻底告别 `AbstractConverter`。仔细看上面两个循环：`!topDownOpt` 只控制了第一个循环里去重集合 `subsets` 的预填充，**第二个注册循环是无条件执行的**！因此哪怕打开了 `topDownOpt`，只要根等价类里存在与目标属性相差 1 个维度的子集（例如逻辑排好序的 `NONE.[0]` 与目标 `ENUMERABLE.[0]` 只差 Convention），根节点上照样会被塞进一个 `AbstractConverter`——后文第七节的真实 Trace 第 2 行就会抓个正着。

---

## 二、撬开搜索空间的第一把钥匙：三值逻辑与 Null-Rejecting 推导

搭好 Hep 与 Volcano 的双引擎骨架之后，在真正钻进代价账本之前，我们先看实战中最能拉开计划质量差距的前置两关：**外连接化简**与**合法连接重排**。如果这两关没打通，后面 CBO 的代价公式算得再精妙，也只能在残缺的搜索空间里打转。

看一条日常业务报表里再普通不过的查询：

```sql
SELECT a.id, b.score
FROM a
LEFT JOIN b ON a.id = b.id
WHERE b.score > 10;
```

左外连接的语义是：当左表 `a` 的某行在右表 `b` 中找不到配对时，在右侧所有列补上 `NULL` 后照常输出。可紧接着上层的 `WHERE` 子句要求 `b.score > 10`——按照 SQL 三值逻辑（`TRUE` / `FALSE` / `UNKNOWN`），把补出来的 `b.score = NULL` 代入 `NULL > 10`，求值结果是 `UNKNOWN`；而 `WHERE` 过滤算子只放行严格为 `TRUE` 的行。这意味着那些辛苦补出来的 `NULL` 行在跨过 `Filter` 时全军覆没，一条也活不下来。既然如此，这层 `LEFT JOIN` 从头到尾就等价于 `INNER JOIN`。

要让优化器自动完成这次推理，核心数学命题只有一个：**当把某侧补空列集合全部代换为 `NULL` 时，目标谓词求值结果是否绝对不可能为 `TRUE`（必然落在 `{FALSE, UNKNOWN}` 中）？** 满足这一性质的谓词，就叫做 **Null-Rejecting（拒绝空值谓词）**。

![Null-Rejecting 三值逻辑推导与 Outer Join 合法重排空间](/images/diagrams/aug-opt-calcite-null-join-reorder.svg)

*图 2：从三值逻辑 Null-Rejecting 证明到合法重排空间的完整决策链——能拒绝补空行的外连接直接降级为内连接解锁全空间交换；必须保留补空语义的外连接则交由 CD-C 超图冲突规则约束枚举，同时严防 Anti Join 与 `<=>` 误删合法空值。*

### 2.1 三套引擎实现 Null-Rejecting 的精妙手法对比

围绕同一个数学命题，Calcite、StarRocks 与 Doris 写出了三种风格迥异的内核实现：

- **Calcite（基于算子策略类的静态归纳）**：[`Strong.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java) 为每种 `SqlKind` 标注了空值传播策略（例如 `Policy.ANY` 表示任一输入为空则结果必为空，如 `+`、`=`、`>`；而 `IS NULL`、`COALESCE`、`IS NOT DISTINCT FROM` 则走定制分支）。调用 `Strong.isNotTrue(expr, nullColumns)` 即可判定给定列集全为空时表达式是否必不为真。在此基础上，[`FilterJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java)（Smart 模式配合 [`RelOptUtil.simplifyJoin()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelOptUtil.java)）先一步将 `LEFT / RIGHT / FULL JOIN` 收紧降级，随后 [`RelMdPredicates`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java) 与 [`JoinPushTransitivePredicatesRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinPushTransitivePredicatesRule.java) 沿着连接等值条件把一侧谓词跨侧传染给另一侧（上游开发线 `68f1613` 还专门修复了 `RelMdPredicates` 中 `RIGHT JOIN` 推导谓词曾错塞进左表列表的归类缺陷）。
- **StarRocks（一站式闭环下推）**：不走独立的元数据总线，直接在重写器 [`JoinPredicatePushdown.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java) 里一气呵成——`convertOuterToInner()` 利用 `Utils.canEliminateNull()` 将双侧均拒空的 `Full Outer` 直降 `Inner`、单侧拒空的分别收紧为 `Left` 或 `Right Outer`，紧接着调用 `rangePredicateDerive()`、`equivalenceDerive()` 与 `deriveIsNotNullPredicate()` 把派生出的非空与范围谓词存入 `OptimizerContext` 供整棵树共享。
- **Doris Nereids（直接代换 `NULL` 字面量做常量折叠）**：[`ExpressionUtils.inferNotNullSlots()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/util/ExpressionUtils.java) 走了一条极其务实的捷径——无需为几百个内置函数单独维护 `Strong` 策略表，直接把表达式里的目标 `Slot` 替换成同类型的 `NULL Literal`，扔给现成的常量折叠器 `FoldConstantRule.evaluate()` 算一把！只要折叠结果是 `NULL` 或 `FALSE`，该列就是铁板钉钉的拒空列（为防超宽表达式组合爆炸，代码对表达式宽度、深度与槽位数设了硬上限）。随后 [`InferFilterNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferFilterNotNull.java)、[`InferJoinNotNull`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/InferJoinNotNull.java) 与 [`EliminateOuterJoin`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java) 在不动点循环中层层剥笋，把嵌套外连接连环降级。

这里藏着两个极易翻车的语义暗礁，正好在上游提交中留下了鲜活教材：

第一，**过滤谓词拒空与聚合函数参数忽略空绝不能共用同一个判据**。在 Doris `7ac0eb2f` 中，研发团队特意把原先统一的判据拆成了两套入口：`Filter` 谓词继续用“折叠结果为 `NULL` 或 `FALSE`”；而针对 `COUNT / SUM` 等忽略空值聚合函数推导非空输入时，新增的 `inferNotNullForNullIgnoringAggregate()` **只承认折叠结果为 `NULL`，绝不承认 `FALSE`**。原因很简单：`FALSE` 在 `WHERE` 里确实留不下行，但 `COUNT(expr)` 里若 `expr` 求值为布尔值 `FALSE`，它是实打实要被计入总数的合法非空值，一旦当成拒空滤掉就会少算行数。

第二，**Semi / Anti Join 与多列空值安全比较的非对称陷阱**。在内连接 `a.k = b.k` 中，顺手给左右两边各补一个 `IS NOT NULL`（如 Calcite 的 [`JoinDeriveIsNotNullFilterRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinDeriveIsNotNullFilterRule.java)）不仅能裁剪扫描页，还能防止海量 `NULL` 键灌进同一个哈希分桶引发严重数据倾斜（Data Skew）。但到了 `LEFT ANTI JOIN`（`NOT EXISTS`），左表里 `a.k = NULL` 的行恰恰因为“跟右表谁都配不上”而成为**必须保留的合法未匹配输出**！因此 Calcite 严格限制 `JoinDeriveIsNotNullFilterRule` 只匹配 `JoinRelType.INNER`，且 `RelMdPredicates` 在 `ANTI` 分支严禁向上透传右表谓词。同样容易写错的还有复合行级空值比较——Calcite 在 `68f1613` 中修掉了 `RelOptUtil.isDistinctFrom()` 的一处陈年 Bug：旧代码展开多字段 `ROW(a, b) IS DISTINCT FROM ROW(c, d)` 时无论正反都用 `AND` 拼接，导致“必须两个字段同时不同才算不同”，修正为 `neg ? AND : OR` 并递归展开嵌套 `struct` 后才恢复了正确的德摩根对偶语义。

---

## 三、带镣铐跳舞：Outer Join Reorder 的合法枚举算法

经过上一轮化简后，如果计划里依然留着无法消除的真·外连接，优化器还能调整连接顺序吗？

在内连接世界里，交换律 \(A \bowtie B = B \bowtie A\) 与结合律 \((A \bowtie B) \bowtie C = A \bowtie (B \bowtie C)\) 畅通无阻；可一旦换成左外连接 \(\mathbin{\text{⟕}}\)，不仅交换律当场失效，连结合律与左交换结合律（Left-Asscom）都是有条件成立的。看下面两组对比：

$$(A \mathbin{\text{⟕}}_{A.k = B.k} B) \bowtie_{B.k = C.k} C = A \bowtie (B \bowtie C)$$

上面这步因为内连接谓词引用并拒绝了 `B.k = NULL`，外连接其实已经被吞掉了；但如果是两个左外连接串联：

$$(A \mathbin{\text{⟕}}_{A.x = B.x} B) \mathbin{\text{⟕}}_{B.y = C.y} C \neq A \mathbin{\text{⟕}}_{A.x = B.x} (B \mathbin{\text{⟕}}_{B.y = C.y} C)$$

随便给一组极简数据就能戳破不等式：令 `A` 为空集、`B = {(1, 2)}`、`C = {(2)}`——左边先做 `A ⟕ B` 得到空集，再 `⟕ C` 还是**空集**；右边先做 `B ⟕ C` 吐出 `(1, 2, 2)`，虽然在这个空 `A` 上仍为空，可一旦令 `A = {(1)}`、`B` 为空集、`C = {(NULL)}` 且外层条件含空值敏感逻辑（或将内层换成反连接或非拒空谓词），两边行数与补空列值立刻分道扬镳。正如论文 [《On the Correct and Complete Enumeration of the Core Search Space》](https://dl.acm.org/doi/10.1145/2463676.2465314)（Moerkotte 等，SIGMOD 2013）系统证明的那样，任何代价模型都只能在**语义合法的候选子集**里挑便宜货——代价算得再准，也救不回一棵语义不等价的错树。

在工程实现上，三套系统沿着两条技术代际给出了各自的答卷：

1. **扁平化启发式与受限变换矩阵（Calcite 传统路径 & StarRocks）**：
   - Calcite 经典的 [`JoinToMultiJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToMultiJoinRule.java) 在拍平连接树时，只拉平内连接骨干，对补空侧（Null-Generating Side）保持边界隔离并登记外连接条件，随后由 [`LoptOptimizeJoinRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/LoptOptimizeJoinRule.java) 构造启发式顺序（若发现补空表连接键唯一且无任何输出列被引用，还能顺手整表裁掉；[`Programs.heuristicJoinOrder()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java) 在表数超阈值时就会切到这条防爆路径）。
   - StarRocks 在 [`JoinReorderProperty.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java) 中硬编码了一张二维连接类型能力矩阵（区分无条件支持、不支持与 `CONDITIONAL_SUPPORTED`），由于尚未在重排矩阵内嵌通用运行时拒空证明，对标记为 `CONDITIONAL_SUPPORTED` 的危险组合一律保守跳过，配合 `QueryOptimizer.memoOptimize()` 的表数分级熔断阀守住规划时延。
2. **基于超图（HyperGraph）与 CD-C 冲突规则的 `DPHyp` 完备枚举（Calcite 实验路径 & Doris Nereids）**：
   - 为了不靠零散规则拼凑而漏掉最优树形，Doris Nereids（[`ConflictRulesMaker.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java) + [`JoinOrderJob.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/JoinOrderJob.java)，大图辅以 `GraphSimplifier` 剪枝，小图与局部变换同时配备 [`OuterJoinLAsscomProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinLAsscomProject.java) 与 [`OuterJoinAssocProject`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/exploration/join/OuterJoinAssocProject.java)）以及 Calcite 的实验特性（[`JoinToHyperGraphRule`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java)，用 64 位 `long` 位图表示最多 64 个关系顶点 + [`ConflictDetectionHelper`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/ConflictDetectionHelper.java) + [`DpHyp`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DpHyp.java)，默认以 `bloat = 127` 限制 DP 表膨胀）双双落地了 **CD-C + DPHyp** 算法：把破坏结合律与交换结合律的顺序约束直接编码成超边（HyperEdge）上的前置位掩码依赖，`DpHyp` 在自底向上枚举连通子图与连通补图对（`CSG-CMP`）时，凡是不满足冲突位掩码的组合在生成前就被一刀切掉，做到既不漏掉合法核心空间、又绝不踩进非法重排雷区。

---

## 四、深入 `VolcanoPlanner` 内部：`RelSubset` 代价账本与 `VolcanoCost` 单维裁判

当逻辑重写与合法重排把候选空间打开之后，真正决定哪棵物理执行树胜出的战场，就是 `VolcanoPlanner` 内部的**代价账本、搜索驱动器与 Trait 补偿协议**。下面这张全景图把这三层核心机制串在了一起：

![VolcanoPlanner 的账本、两种 Driver 与 Trait 补偿](/images/calcite-cost-traits.svg)

*图 3：从上到下依次为两套驱动器共用的 `RelSubset` 账本、默认的自底向上穷举路径（蓝）与可选的自顶向下分支限界路径（橙），以及底部的 Trait 补偿与指针提取机制。*

先掀开 [`RelSubset.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)，看看这张被两条搜索路径共用的核心账本到底记了哪七笔账：

| 账本字段 | 类型与初值 | 真实工程语义 | 谁负责读写 |
| --- | --- | --- | --- |
| `best` | `@Nullable RelNode` | 当前能满足该 `RelSubset` 属性要求的最便宜物理算子 | `propagateCostImprovements` 写入，`buildCheapestPlan` 读取 |
| `bestCost` | `RelOptCost`（初值 `infCost`） | `best` 算子连同其全部子树的**累计总代价** | 同上 |
| `timestamp` | `int` | 每次 `best` 或 `bestCost` 变动时自增，用于触发 Metadata 缓存失效 | `propagateCostImprovements` |
| `upperBound` | `RelOptCost`（初值 `infCost`） | 分支限界搜索允许的成本天花板 | **仅 `TopDownRuleDriver` 读写** |
| `taskState` | `@Nullable OptimizeState` | 标记 `OPTIMIZING` 或 `COMPLETED` | **仅 `TopDownRuleDriver` 读写** |
| `state` | `int` 位掩码 | 区分属性是已由下层交付（`DELIVERED=1`）还是由上层索要（`REQUIRED=2`） | `setDelivered` / `setRequired`（服务 Top-Down） |
| `enforceDisabled` | `boolean` | 禁止对天然已满足属性的自身子集重复补插 Enforcer | `disableEnforcing()`（服务 Top-Down） |

表格下半部分的四列字段道破了天机：Calcite 在同一个 `RelSubset` 对象里兼容了两代搜索引擎。跑默认的 `IterativeRuleDriver` 时，后面四个字段全程沉睡——跑完全程，`upperBound` 依然是无穷大、`taskState` 依然是 `null`，没有任何分支因为“太贵”被提前截断；真正干活的只有上半部分的 `best`、`bestCost` 与向上冒泡堆。

这也立刻解开了开头的谜团：为什么 `findBestExp()` 第 ⑤ 步调 `buildCheapestPlan()` 提取最优计划时，代码里连一次代价大小比较都没有？因为**代价竞争早在第 ①～③ 步每一次新算子注册进 `RelSubset` 的瞬间，就已经实时结算并冒泡传播完毕了**；`ruleDriver.drive()` 只管催生新候选，账本自己会随时刷新擂主。

### 4.1 局部代价、累计代价与自底向上的堆冒泡

在算账时，单算子自身开销由 `RelMetadataQuery.getNonCumulativeCost(rel)`（底层调各算子的 `computeSelfCost`）给出，而整棵子树的累计总账则由 `VolcanoPlanner.getCost(rel, mq)` 汇总：

```java
@Override public @Nullable RelOptCost getCost(RelNode rel, RelMetadataQuery mq) {
  if (rel instanceof RelSubset) {
    return ((RelSubset) rel).bestCost;          // ① 撞上 RelSubset 立即停步，直接取账本快照！
  }
  if (noneConventionHasInfiniteCost
      && rel.getTraitSet().getTrait(ConventionTraitDef.INSTANCE) == Convention.NONE) {
    return costFactory.makeInfiniteCost();      // ② 逻辑节点一律判为无穷贵，倒逼落成物理算子
  }
  RelOptCost cost = mq.getNonCumulativeCost(rel);
  if (cost == null) {
    return null;
  }
  if (!zeroCost.isLt(cost)) {
    cost = costFactory.makeTinyCost();          // ③ 非正局部代价强行抬成 TINY = (1, 1, 0)
  }
  for (RelNode input : rel.getInputs()) {
    RelOptCost inputCost = getCost(input, mq);
    if (inputCost == null) {
      return null;
    }
    cost = cost.plus(inputCost);
  }
  return cost;
}
```

这段不到三十行的代码里藏着三处极易踩坑的关节：

- **关节 ①（截断递归）**：算父节点累计代价时绝不会穿透进孙辈遍历，碰到输入 `RelSubset` 就直接读它的 `bestCost` 缓存。因此如果子 `RelSubset` 后来找到了更便宜的物理实现，必须有人主动沿着父引用往上通知刷新。
- **关节 ②（无穷大防火墙）**：所有 `Convention.NONE` 的逻辑算子代价恒为 `infinite`。哪怕逻辑树再短，也休想混进最终物理计划，同时这也埋下了一个伏笔——逻辑节点无法直接提供有限的成本下界。
- **关节 ③（隐蔽的 `TINY` 抹平效应）**：如果某个自定义算子返回了 `(0 rows, 1e9 cpu, 0 io)` 的自身代价，因为后面马上会看到的 `zeroCost.isLt(cost)` 只比对 `rowCount`，`0 < 0` 为假，这个算子的代价会被当成“非正数”强行替换成 `TINY = (1, 1, 0)`——那 `1e9` 的巨额 CPU 开销当场人间蒸发。

每当有新物理算子注册进来，`propagateCostImprovements` 就会启动一个按代价排序的最小堆，自底向上把降价喜讯传遍全图：

```java
while ((relNode = propagateHeap.poll()) != null) {
  RelOptCost cost = requireNonNull(propagateRels.get(relNode), ...);
  for (RelSubset subset : getSubsetNonNull(relNode).set.subsets) {
    if (!relNode.getTraitSet().satisfies(subset.getTraitSet())) {
      continue;                                  // 属性不达标，没资格竞选该分桶的擂主
    }
    if (relNode != subset.best && !cost.isLt(subset.bestCost)) {
      continue;                                  // 不比现有擂主便宜，跳过
    }
    if (relNode == subset.best && cost.equals(subset.bestCost)) {
      continue;
    }
    subset.timestamp++;
    subset.bestCost = cost;
    subset.best = relNode;
    mq.clearCache(subset);                       // 账本刷新，立刻清空子集与父节点的元数据缓存
    for (RelNode parent : subset.getParents()) {
      mq.clearCache(parent);
      RelOptCost newCost = getCostOrInfinite(parent, mq);
      ...                                        // 父算子跟着变便宜了，继续入堆向上冒泡！
    }
  }
}
```

注意外层 `for` 循环遍历的是同一个 `RelSet` 下的**所有 `subsets`**：只要一个物理算子交付的属性足够强（例如 `EnumerableMergeJoin` 同时交付了 `[[0], [3]]` 排序属性），`satisfies` 检查就能一路绿灯，让它**同时当上 `ENUMERABLE.[[0],[3]]`、`ENUMERABLE.[0]` 和无序 `ENUMERABLE.[]` 多个分桶的 `best` 擂主**！另外源码注释还坦诚交代过一桩奇案（见 `JdbcAdapterTest#testVolcanoPlannerInternalValid`）：在特定元数据动态变化下，原擂主重算出的代价甚至可能微弱上涨，因此当 `relNode == subset.best` 时必须无条件覆盖更新，并连调两次 `mq.clearCache` 保证元数据缓存与账本同步。

等搜索收工，`CheapestPlanReplacer` 从根 `RelSubset` 出发，机械地把沿途每个 `RelSubset` 替换成它的 `best` 节点即可。万一走到某个急需的 `RelSubset` 发现 `best == null`，就会转交 `DeadEndFinder` 抛出所有 Calcite 用户都见过的噩梦异常 `CannotPlanException`（*"There are not enough rules to produce a node with desired properties"*）——翻译成大白话就是：**账本上有个分桶直到散场都没人填上有限代价的物理算子。**

### 4.2 `VolcanoCost` 源码里的 `if (true)`：为什么 CPU 和 I/O 只是摆设？

既然 `propagateCostImprovements` 全靠 `cost.isLt(subset.bestCost)` 决定谁当擂主，那我们就得打开默认的 [`VolcanoCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoCost.java) 看看它是怎么比大小的：

```java
@Override public boolean isLe(RelOptCost other) {
  VolcanoCost that = (VolcanoCost) other;
  if (true) {
    return this == that || this.rowCount <= that.rowCount;
  }
  return (this == that)
      || (this.rowCount <= that.rowCount
          && this.cpu <= that.cpu
          && this.io <= that.io);      // 永远执行不到的死代码
}

@Override public boolean isLt(RelOptCost other) {
  if (true) {
    VolcanoCost that = (VolcanoCost) other;
    return this.rowCount < that.rowCount;
  }
  return isLe(other) && !equals(other);   // 永远执行不到的死代码
}
```

你没看花眼，这两个 `if (true)` 在上游源码里原封不动躺了多年！`VolcanoCost` 虽然在内部老老实实维护了 `(rowCount, cpu, io)` 三个双精度浮点数，在做加减乘法和打印 `EXPLAIN` 时也逐维计算，但在决定生死的 `isLe` 和 `isLt` 里，**`cpu` 和 `io` 完完全全是只记账、不判案的“装饰列”**。

这行代码直接引发了四个深远后果：

1. 如果不传入自定义的 `RelOptCostFactory` 替换掉默认的 `VolcanoCost.FACTORY`，你在算子 `computeSelfCost` 里花再大心血调优 CPU 和 I/O 公式，都不会改变任何计划的胜负；
2. 只要两条候选计划累计处理的总行数（`rowCount`）相等，裁判就判平局——谁先被搜索引擎枚举出来抢占了 `best`，谁就笑到最后（第七节的真实运行示例就会撞上这一幕）；
3. `upperBound.minus(...)` 做上界扣减时是三个维度各自相减的，哪怕 `cpu` 维度减穿成了荒谬的负数，只要 `rowCount` 还是正的，比较器就浑然不觉；
4. 反过来，一旦你在自己的引擎里接管了多维代价比较，就必须先去审计 Top-Down 路径里的 `minus` 减法逻辑，否则负 CPU 分量会当场把剪枝逻辑搞崩。

---

## 五、两种搜索驱动器：84 行穷举队列与 11 类任务的分支限界栈

看懂了底层的共享账本，再向上看 `findBestExp()` 第 ③ 步的 `ruleDriver.drive()`，两条搜索路径的分野就一目了然了。切换开关是系统属性 `calcite.planner.topdown.opt`（默认 `false`；走 JDBC 连接串可通过 `topDownOpt=true` 触发 `CalcitePrepareImpl.setTopDownOpt`，而直接用 `Frameworks` / `PlannerImpl` 编程接入时没有透传该连接参数，只能靠 JVM 启动参数 `-Dcalcite.planner.topdown.opt=true` 或手动调 `planner.setTopDownOpt(true)` 开启）。

### 5.1 默认路径 `IterativeRuleDriver`：极简自底向上穷举

翻开 [`IterativeRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleDriver.java)，核心逻辑简短到令人惊讶：

```java
@Override public void drive() {
  while (true) {
    LOGGER.debug("Best cost before rule match: {}", planner.root.bestCost);
    VolcanoRuleMatch match = ruleQueue.popMatch();
    if (match == null) {
      break;
    }
    try {
      match.onMatch();
    } catch (VolcanoTimeoutException e) {
      planner.canonize();
      break;
    }
    planner.canonize();
  }
}
```

早年 Calcite 曾尝试过按子集“重要性（Importance）”动态排序规则队列，后来因维护复杂且易引发饥饿被彻底删掉。如今的 `IterativeRuleQueue` 就是纯粹的先进先出（FIFO）队列——只把确定性的等价替换规则（`SubstitutionRule`）放进优先队列 `preQueue` 先跑，其余变换与实现规则一视同仁塞进普通 `queue` 顺序弹出。整个循环除了规则耗尽或触发 `VolcanoTimeoutException` 超时异常外绝不停歇，完全依靠 `RelDigest` 结构去重和 `skipMatch()` 环路检测控住规模，把所有能触发的候选通通塞进账本比一遍大小。

### 5.2 可选路径 `TopDownRuleDriver`：Cascades 任务栈与栈序反转技巧

当你把 `topDownOpt` 拨到 `true`，接管比赛的就是实现了完整 Cascades 协议的 [`TopDownRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleDriver.java)。它用一个后进先出栈（1.43 开发线已将过时的 `Stack<Task>` 替换为 `ArrayDeque<Task>`，队首 `push/pop` 顺序完全一致）驱动 11 类精细任务：

| 任务类别 | 包含的具体任务与分工 |
| --- | --- |
| **子集生命周期** | `OptimizeGroup`（入口检查已有 Winner，否则压入组内表达式任务）、`GroupOptimized`（收尾打上 `taskState = COMPLETED` 印章） |
| **逻辑空间探索** | `OptimizeMExpr`（区分 `explore` 仅做逻辑扩展与完整实现两模式）、`ExploreInput` + `EnsureGroupExplored`（深入并回收子组的逻辑变换） |
| **规则精准触发** | `ApplyRules`（探索期仅放行 `isTransformationRule`，实现期全放开）、`ApplyRule`（执行单次规则匹配） |
| **物理输入寻优与剪枝** | `OptimizeInput1`（单输入快路径）、`OptimizeInputs`（多输入有状态推进，逐个孩子收紧预算）、`CheckInput`（校验子 Winner，失败则连坐放弃兄弟分支） |
| **属性自底向上反哺** | `DeriveTrait`（当子节点交付了比要求更强的属性时，唤醒父物理算子推导新形态） |

读 `TopDownRuleDriver` 源码时，有两处**代码字面写法与实际执行先后完全相反**的压栈技巧千万别看反了。

第一处是 `OptimizeGroup.perform()` 里对补偿算子（Enforcer）的降权处理：

```java
List<RelNode> physicals = new ArrayList<>();
for (RelNode rel : group.set.rels) {
  if (planner.isLogical(rel)) {
    tasks.push(new OptimizeMExpr(rel, group, false));
  } else if (rel.isEnforcer()) {
    // Enforcers have lower priority than other physical nodes.
    physicals.add(0, rel);
  } else {
    physicals.add(rel);
  }
}
for (RelNode rel : physicals) {
  tasks.push(...);
}
```

看到 `physicals.add(0, rel)` 插到列表最前面，第一反应往往是“让 Enforcer 优先”。但别忘了紧接着有一个循环把 `physicals` 依次 `push` 进后进先出的 `tasks` 栈！排在索引 `0` 的 Enforcer 最先入栈被压在栈底，反而是排在后面的真实物理算子最后入栈、最先弹出执行。为什么要这么干？注释一语中的：**必须先跑真实物理算子尽快算出一个有限的 `upperBound`，如果让代价无穷大的占位 Enforcer 先跑，上界永远是 `inf`，剪枝根本启动不了。** 同样的栈反转技巧也出现在 `TopDownRuleQueue` 里：`SubstitutionRule` 被 `addLast` 放到双端队列尾部，只因弹出消费时从尾部先取，从而保证等价替换先于物理实现执行。

有了显式任务栈，上下界剪枝闭环才能真正运转起来：

1. **严苛的 Winner 认证（`RelSubset.getWinnerCost`）**：在 Top-Down 模式下，账本上有 `best` 绝不等于大功告成——必须同时满足 **`taskState == OptimizeState.COMPLETED` 且 `bestCost.isLe(upperBound)`**，才算拿到合法 Winner。
2. **仅对物理节点生效的下界估计（`RelMdLowerBoundCost`）**：剪枝不仅需要上界，还需要估算“还没优化的兄弟分支至少要花多少钱（Lower Bound）”。翻开 [`RelMdLowerBoundCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdLowerBoundCost.java) 可以看到，只要遇到逻辑节点（`planner.isLogical(node)`），它直接返回 `null`！也就是说，在物理候选真正浮出水面之前，Calcite 并不会用逻辑统计基数提前算下界剪枝；只有当物理算子登场后，它才累加算子自身非无穷代价与各输入的已知下界。
3. **逐个孩子收紧预算（`OptimizeInputs` + `CheckInput`）**：当多输入物理算子（如 Join）依次优化第 `i` 个孩子时，分给它的成本天花板由 `upperBoundForInputs` 扣掉父算子自身代价与其余兄弟孩子的下界之和给出：

$$\text{UB}(\text{child}_i) = \text{UB}(\text{subset}) - \text{SelfCost}(\text{parent}) - \sum_{j \neq i} \text{LB}(\text{child}_j)$$

一旦第 `0` 号孩子顺利拿回真实 Winner，`CheckInput` 立刻用这笔更紧的实付账单替换原先粗糙的下界，把第 `1` 号孩子的预算压得更低；反过来，要是第 `0` 号孩子在当前预算下空手而归（无解），`CheckInput` 直接把 `lowerBoundSum` 设为无穷大，当场赦免第 `1` 号孩子的无谓搜索。

---

## 六、Trait 补偿协议：`AbstractConverter` 占位符与 `PhysicalNode` 按需传导

在两种驱动器差异最大的地方，除了剪枝，就是**当现有算子满足不了目标物理属性（Trait）时，怎么补救？**

先看 Calcite 判定属性满足的底层规则。打开 [`RelTraitSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelTraitSet.java) 的 `satisfies` 方法，循环上限写的是 `Math.min(this.size(), that.size())`——较短集合里未声明的尾部维度会被视为自动满足，且包含复合排序属性 `RelCompositeTrait` 时需先经 `simplify()` 折叠。在每个 `RelSubset` 上，`state` 位掩码精细地区分了两种身份：下层算子真实产出的属性调用 `setDelivered()`（首次交付时点亮 `triggerRule = true` 唤醒规则匹配），而上层算子伸手索要的属性调用 `setRequired()`（只立搜索靶子，绝不虚触发规则；若推导出的新需求恰好落回孩子原本所在的 `subset`，还会调 `disableEnforcing()` 关掉无意义的自我补偿）。

真正的分水岭位于 [`RelSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSet.java) 的 `addConverters` 调用处：

```java
addConverters(subset, required, !planner.topDownOpt);
```

第三个布尔参数名叫 `useAbstractConverter`，传入的值恰恰是 `!planner.topDownOpt`！这一刀直接切出了两套截然不同的补偿哲学：

| 对照维度 | 默认自底向上路径（`topDownOpt = false`） | 可选自顶向下路径（`topDownOpt = true`） |
| --- | --- | --- |
| **注入的补偿节点** | `new AbstractConverter(cluster, from, null, toTraitSet)` | 调用目标约定的 `convention.enforce(from, toTraitSet)` |
| **自身代价与可执行性** | `makeInfiniteCost()`，纯逻辑占位符，绝不可能被直接选中 | 如 `EnumerableSort`，直接携带有限真实物理代价参与打擂 |
| **如何落地成真算子** | 靠内嵌的 `ExpandConversionRule` 调 `changeTraitsUsingConverters` 展开 | 生下来就是真算子，无需二次规则展开 |

为什么默认的自底向上路径非要绕 `AbstractConverter` 这个大弯子？因为在没有自顶向下任务栈的情况下，子算子是先于父需求被盲目枚举出来的；为了把“`ENUMERABLE` 无序子集”和父节点索要的“`ENUMERABLE.[0]` 有序子集”搭上线，`RelSet` 只能先在两者之间塞进一个无穷代价的 `AbstractConverter` 当引线，等队列排到 `ExpandConversionRule` 时，再调用 `TraitDef.convert()`（例如在 [`CollationConversionTest.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/test/java/org/apache/calcite/plan/volcano/CollationConversionTest.java) 中验证的转出 `PhysicalSort`）把它替换成真正的排序算子。正如 `AbstractConverter` 源码注释自嘲的那样（*"AbstractConverters can be messy, so they restrain themselves"*），一旦处理不当极易引发转换器组合爆炸，所以它给自己加了一条紧箍咒：如果目标 `RelSubset` 里已经有了一个代价小于无穷大的现成实现，它就克制住自己不再触发。

而一旦切到 `topDownOpt = true`，[`EnumerableConvention.enforce()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableConvention.java) 直接一步到位：Convention 不对就调转换器，缺 `RelCollation` 排序就直接 `EnumerableSort.create(...)` 造出真算子扔进账本比价。

更进一步，Calcite 没有像 StarRocks 那样写一个包揽全局算子的中央 `RequiredPropertyDeriver`，而是把双向属性传导契约下放到了每个物理算子自己实现的 [`PhysicalNode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/PhysicalNode.java) 接口上：

- **自顶向下传需求（`passThroughTraits`）**：父节点带着排序要求敲门时，算子自己判断能不能接住并翻译成对输入子集的要求。比如 [`EnumerableMergeJoin.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoin.java) 检查父要求的排序列：如果恰好等于左连接键集合，就原样下压给左输入并映射成右连接键排序压给右输入；如果是左连接键的前缀子集，就用 `extendCollation` 补全连接键后下压；否则干脆返回 `null` 拒绝。
- **自底向上推能力（`deriveTraits` + `DeriveMode`）**：当某个子集产出了新的排序属性时，`DeriveTrait` 任务按算子声明的 [`DeriveMode`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/DeriveMode.java)（`LEFT_FIRST`、`RIGHT_FIRST`、`BOTH`、`OMAKASE` 或 `PROHIBITED`）回调父算子。`EnumerableMergeJoin` 声明的是 **`DeriveMode.BOTH`**——左右任一侧交付了覆盖连接键的排序属性，它都能顺势推导出另一侧所需的配对排序并生成新的有序 Merge Join 变体（顺带一提，1.43 开发线修正了其 `implement()` 代码生成中单列布尔连接键被误推断为 `Predicate1` 的问题，显式锁定了 `Function1.class`）。

把自顶向下压需求、缺失属性时的 `Convention.enforce()` 补偿，以及下层天然有序或按分桶分布时的自底向上反哺串在一条时间线上，整套 `PhysicalNode` 协商时序一目了然：

![Calcite Top-Down 模式下 PhysicalNode 双向 Trait 协商与补偿](/images/diagrams/opt-calcite-trait-negotiation.svg)

*图 4：`TopDownRuleDriver` 驱动的双向 Trait 协商——上层通过 `passThroughTraits` 向子 `RelSubset` 下压排序/分布需求并由 `Convention.enforce()` 兜底；下层若天然交付索引顺序或分桶属性，则经 `DeriveTrait` 回调 `deriveTraits` 派生出免除冗余 `Sort`/`Exchange` 的父算子新变体。*

---

## 七、真实运行 Trace 走读：`Join + ORDER BY` 在两种驱动器下的平局与负 CPU 现场

纸上谈兵不如实跑一轮。我们用 `ReflectiveSchema` 构造一个完全可复现的内存测试集：员工表 `emps` 含 `10,000` 行（`deptno = i % 10`），部门表 `depts` 含 `10` 行，运行一条最普通的连接排序查询：

```sql
SELECT e.deptno, e.ename, d.dname
FROM emps e JOIN depts d ON e.deptno = d.deptno
ORDER BY e.deptno;
```

并通过 `log4j.logger.org.apache.calcite.plan.volcano.task=DEBUG` 抓取 `CalciteTrace.getPlannerTaskTracer()` 的完整内部轨迹。送进 `VolcanoPlanner` 前，两条路径面对的初始逻辑计划一模一样：

```text
LogicalSort(sort0=[$0], dir0=[ASC])
  LogicalProject(deptno=[$2], ename=[$1], dname=[$4])
    LogicalJoin(condition=[=($2, $3)], joinType=[inner])
      LogicalTableScan(table=[[hr, emps]])
      LogicalTableScan(table=[[hr, depts]])
```

跑完 `findBestExp()`，看看两种驱动器各自交出的物理计划答卷：

- **默认驱动器（`topDownOpt = false`）选出的物理树（大表 `emps` 在左，`depts` 在右）**：

```text
EnumerableCalc(deptno=[$t1], ename=[$t0], dname=[$t3])
    : cumulative cost = {70030.0 rows, 2007540.5914138353 cpu, 0.0 io}
  EnumerableMergeJoin(condition=[=($1, $2)], joinType=[inner])
      : cumulative cost = {55030.0 rows, 1902540.5914138353 cpu, 0.0 io}
    EnumerableSort(sort0=[$1], dir0=[ASC])
        : cumulative cost = {30000.0 rows, 1902069.0743952366 cpu, 0.0 io}
      EnumerableCalc(ename=[$t1], deptno=[$t2])
          : cumulative cost = {20000.0 rows, 60001.0 cpu, 0.0 io}
        EnumerableTableScan(table=[[hr, emps]])
            : cumulative cost = {10000.0 rows, 10001.0 cpu, 0.0 io}
    EnumerableSort(sort0=[$0], dir0=[ASC])
        : cumulative cost = {20.0 rows, 471.5170185988092 cpu, 0.0 io}
      EnumerableTableScan(table=[[hr, depts]])
          : cumulative cost = {10.0 rows, 11.0 cpu, 0.0 io}
```

- **自顶向下驱动器（`topDownOpt = true`）选出的物理树（小表 `depts` 被换到了左边！）**：

```text
EnumerableCalc(deptno=[$t3], ename=[$t2], dname=[$t1])
    : cumulative cost = {70030.0 rows, 2007540.5914138353 cpu, 0.0 io}
  EnumerableMergeJoin(condition=[=($0, $3)], joinType=[inner])
      : cumulative cost = {55030.0 rows, 1902540.5914138353 cpu, 0.0 io}
    EnumerableSort(sort0=[$0], dir0=[ASC])          ← 10 行的 depts 跑到了左侧
        : cumulative cost = {20.0 rows, 471.5170185988092 cpu, 0.0 io}
      EnumerableTableScan(table=[[hr, depts]])
    EnumerableSort(sort0=[$1], dir0=[ASC])          ← 10000 行的 emps 跑到了右侧
        : cumulative cost = {30000.0 rows, 1902069.0743952366 cpu, 0.0 io}
      EnumerableCalc(ename=[$t1], deptno=[$t2])
        EnumerableTableScan(table=[[hr, emps]])
```

对比这两棵树，前面拆解的所有源码机制瞬间全部对上了号：

1. **顶层 `LogicalSort` 凭空消失了**：无论是哪条路径，双侧输入经 Enforcer 补上 `EnumerableSort` 喂给 `EnumerableMergeJoin` 后，Merge Join 吐出的有序属性直接满足了根节点的 `ORDER BY e.deptno` 要求，省掉了最外层的二次排序。
2. **一分不差的代价平局与先到先得**：由于内连接交换左右输入后，双侧扫描、计算和排序的累计数字完全对称（总账都是 `70030.0 rows, 2007540.59 cpu`），两条计划在账本上打成绝对平手；默认队列先枚举出“`emps` 在左”，而 Top-Down 栈因交换律与 `DeriveMode.BOTH` 的推进次序先锁定了“`depts` 在左”，后续平局候选无法满足 `cost.isLt(subset.bestCost)` 的严格小于号，于是各自保留了先手擂主。

再翻开 Top-Down 模式吐出的 **410 行 `Execute task` 真实运行日志**（其中真正执行规则变换的 `ApplyRule` 只有 41 次，其余 90% 的任务都在做精细的组状态推进、输入预算切割和属性反哺），五个阶段的演化轨迹纤毫毕现：

```text
// 阶段 ①：根组启动，ensureRootConverters() 塞进来的根 AbstractConverter 当场现身！
Execute task: OptimizeGroup(group=rel#41:RelSubset#5.ENUMERABLE.[0], upperBound={inf})
Execute task: OptimizeInput1(mExpr=rel#42:AbstractConverter.ENUMERABLE.[0](
                 input=RelSubset#36,convention=ENUMERABLE,sort=[0]), upperBound={inf})
Execute task: OptimizeGroup(group=rel#36:RelSubset#5.NONE.[0], upperBound={inf})
Skip optimizing because of traits: rel#42:AbstractConverter.ENUMERABLE.[0](...)

// 阶段 ②：explore=true，自顶向下深入叶子，仅触发 JoinCommuteRule 等纯逻辑变换规则
Execute task: OptimizeMExpr(mExpr=rel#31:LogicalJoin.NONE.[](...), explore=true)
Execute task: ExploreInput(group=rel#29:RelSubset#1.NONE.[])
Execute task: ApplyRules(mExpr=rel#31:LogicalJoin.NONE.[](...), exploring=true)

// 阶段 ③：首个物理连接 EnumerableHashJoin 登场，在无穷上界 {inf} 下摸底建立首个可行成本
Execute task: OptimizeInputs(mExpr=rel#74:EnumerableHashJoin.ENUMERABLE.[](...),
                 upperBound={inf}, processingChild=0)

// 阶段 ④：有限上界正式激活！第 0 号孩子解出后，第 1 号孩子的上界收紧至 45033.02 rows
Execute task: OptimizeInputs(mExpr=rel#91:EnumerableMergeJoin.ENUMERABLE.[[0], [3]](
                 left=RelSubset#87,right=RelSubset#89,...),
                 upperBound={45033.02585092994 rows, 30012.0 cpu, 0.0 io}, processingChild=1)

// ★ 抓获负 CPU 案发现场：upperBound.minus 逐维相减产生 -29527 与 -4008165 的负 CPU！
Execute task: OptimizeGroup(group=rel#98:RelSubset#3.ENUMERABLE.[2],
                 upperBound={30044.02585092994 rows, -29527.48298140119 cpu, 0.0 io})
Execute task: OptimizeGroup(group=rel#58:RelSubset#6.ENUMERABLE.[],
                 upperBound={30044.02585092994 rows, -4008165.7846168266 cpu, 0.0 io})

// 阶段 ⑤：DeriveTrait 上拉属性，ExpandConversionRule 展开根转换器，根组 COMPLETED 收官
Execute task: DeriveTrait(mExpr=rel#95:EnumerableMergeJoin.ENUMERABLE.[0](...),
                 group=rel#96:RelSubset#6.ENUMERABLE.[0])
Execute task: ApplyRule(match=rule [ExpandConversionRule] rels [#42], exploring=false)
Execute task: GroupOptimized(group=rel#41:RelSubset#5.ENUMERABLE.[0], upperBound={inf})
```

盯着阶段 ④ 那两行刺眼的 `cpu = -29527.48` 和 `cpu = -4008165.78` 看三秒钟——下界估算与上界逐维减法在 `cpu` 维度上早就穿仓成了负数，只因为 `VolcanoCost.isLe` 里的那个 `if (true)` 蒙着眼睛只看 `rowCount`，整台机器才安然无恙地跑到了终点。

---

## 八、出了 Memo 之后：物理后处理、执行边界与工程落地指南

当 `VolcanoPlanner.findBestExp()`（或 StarRocks `extractBestPlan`、Doris `chooseBestPlan`）从根等价组里捞出总成本最低的那棵物理算子树时，规划工作结束了吗？恰恰相反，工业界优化器在这之后全都排着一长串**物理后处理**（Post-CBO Physical Rewrite）流水线。

为什么不在 Memo 里一步到位算完所有物理细节？算一笔搜索复杂度账就明白了：像 **Runtime Filter 拓扑穿线、低基数全局字典延迟解码（Decode）、全局延迟物化（Global Late Materialization，简称 GLM）、局部公共子表达式抽取以及 Fragment 管道切分**，它们要么强依赖“谁是 Build、谁是 Probe、哪里插了 Shuffle Exchange”的最终定型拓扑，要么如果在 Memo 里给成千上万个半成品候选逐一挂载，会把规划耗时直接拖垮十倍以上。

因此三套系统都在 CBO 出口之后设立了专门的“精装修阶段”：

- **Calcite [`Programs.standard()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java)**：在 `findBestExp()` 之后紧跟第二轮 Hep `calc()` 通道做物理表达式合并与微调（源码称之为物理 `tweaks`）；
- **StarRocks [`QueryOptimizer`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java)**：依次跑 `physicalRuleRewrite()`（冗余 Shuffle 裁剪、流式 `SortAgg` 标记、低基数字典插入、谓词重排、`WidenProjectionNullableRule` 对齐槽位可空性、最后运行 `GlobalLateMaterializationRewriter` 插入 `FETCH` 算子）与 `dynamicRewrite()`（倾斜洗牌消除与历史调优指南应用）；
- **Doris [`PlanPostProcessors.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java)**：在 [`NereidsPlanner`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java) 选出最优树后，串行完成单调函数分区裁剪推导（`AddMonotonicFunctionPruningPredicates`）、公共子表达式消除、TopN 延迟物化、Fragment 切分以及 `RuntimeFilterGenerator` 穿线与无效剪枝。

这里必须恪守一条铁律：**CBO 后处理只能做不推翻全局代价契约的局部增效手术，并且必须严守执行器的物理类型契约**（例如 StarRocks 主干特意规定 `WidenProjectionNullableRule` 必须在全局延迟物化之前跑，因为 `FETCH` 算子直接从存储层往目标 Slot 填数据，倘若上层投影把列的可空性收窄了，延迟物化就会生成可空性不匹配的非法内存引用）。

顺着物理后处理再往前迈一步，就触及了很多人最关心的边界问题：**运行时统计反馈与 Dynamic Filter，到底归不归 Calcite 管？** 先把经常被混为一谈的四类“统计与动态优化”按时间轴切开：

| 优化机制 | 事实产生时机 | 作用半径 | 是否重构当前查询的 Join 树？ |
| --- | --- | --- | --- |
| **静态统计信息（Static Stats）** | 查询规划前 | 当前查询 CBO 计价 | 是，编译期敲定物理树 |
| **历史执行反馈（HBO / Tuning Guide）** | 上一次查询跑完后 | **后续**同骨架相似查询 | 是，纠偏下一次规划的基数或算子选型 |
| **运行时过滤（Runtime / Dynamic Filter）** | 当前查询 Build 端跑完瞬间 | 当前查询 Probe 端未读数据 | **否**，保持原树不变，走旁路摘要裁剪 |
| **执行中途重优化（Mid-Query Re-opt / AQE）** | 当前 Shuffle / Stage 物化边界 | 当前查询尚未启动的上游阶段 | 是，需运行期中断并迁移中间状态 |

翻遍 Calcite Core 的 `core/src/main/java`，你找不到任何一个分布式 `RuntimeFilter` 执行协议、跨节点 Bloom 合并器或者执行中途挂起流水线的状态机——这绝不是 Calcite 的缺陷，而是**可嵌入规划库与宿主执行内核的天然楚河汉界**：

![Calcite 的职责边界：规划、执行与反馈各自由谁负责](/images/calcite-feedback-boundary.svg)

*图 5：Calcite Core 与宿主引擎的职责切分——Calcite 的能力停留在规划侧接口（`RelMetadataProvider` 统计注入点、`Context` 参数通道、自定义物理 `RelNode` 与 `Convention.enforce()`）；而 Build 端键值收集、跨节点合并投递、Scan 页级下推以及 Profile 历史统计回流（如 StarRocks [`PlanTuningAdvisor`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/feedback/PlanTuningAdvisor.java)、Doris [`HboPlanStatisticsProvider`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboPlanStatisticsProvider.java) + [`HboStatsCalculator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboStatsCalculator.java)），必须由拥有网络交换与存储扫描器的宿主引擎闭环实现。*

### 8.1 三套优化器全景对照：通用框架 vs 垂直一体化 MPP 内核

走完全链路，把 Apache Calcite 与系列第 3 篇的 [StarRocks Optimizer]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}}) 及 Apache Doris Nereids 摆在同一张对比表里，每一处架构取舍都清晰可见：

| 对比维度 | Apache Calcite (`Hep` + `Volcano`) | StarRocks FE Optimizer | Doris Nereids |
| --- | --- | --- | --- |
| **核心架构定位** | 多数据源可嵌入代数与搜索框架 | 垂直整合 MPP 原生优化器 | 垂直整合 MPP 原生优化器 |
| **算子与标量表示** | `RelNode` / `RexNode` | `Operator` / `ScalarOperator` | `Plan` / `Expression` |
| **启发式重写层（RBO）** | `HepPlanner` + `HepProgram` | 树重写任务栈 + 分阶段 `RuleSet` | `Rewriter` + `RewriteJob` |
| **物理属性子目标表示** | `RelSubset = RelSet × RelTraitSet`（编码进算子节点） | `Group.lowestCostExpressions`（按 `PhysicalPropertySet` 哈希分桶） | `Group` + `lowestCostPlans[PhysicalProperties]` |
| **统计与元数据传递** | 动态分派总线 `RelMetadataQuery` | 内置 `StatisticsCalculator` 与属性推导器 | 内置 `StatsCalculator` 与 `DataTrait` |
| **默认代价裁判** | `VolcanoCost` 硬编码只比对 `rowCount` | `realCost = 0.5·cpu + 2.0·mem + 1.5·net` 多维标定 | CPU / Memory / Network 加权多维代价模型 |
| **分支限界剪枝** | 默认驱动器零剪枝；仅 `topDownOpt=true` 且物理候选出现后生效 | `EnforceAndCostTask` 原生内置上界收紧与 `costLowerBounds` 拦截 | Cascades 任务栈原生支持上下界剪枝 |
| **属性双向传导位置** | 分散在各物理算子的 `PhysicalNode.passThrough* / derive*` | 集中式 `RequiredPropertyDeriver` + `OutputPropertyDeriver` + `Guarantor` | `RequestPropertyDeriver` + `ChildOutputPropertyDeriver` + `EnforceMissingPropertiesHelper` |
| **Enforcer 代价形态** | 默认路径先塞无穷代价 `AbstractConverter`，再靠规则二次展开；Top-Down 走 `Convention.enforce()` | `Exchange` / `Sort` 直接以真实物理算子计价入账 | `Distribution` / `Order` Enforcer 直接计价入账 |
| **Null-Rejecting 证明** | `Strong` 算子策略表 + 谓词元数据 | `Utils.canEliminateNull` + `JoinPredicatePushdown` | 槽位代换 `NULL` 后常量折叠（区分谓词与聚合参数） |
| **Outer Join 重排** | `MultiJoin + Lopt`；实验级 CD-C `DPHyp` | 二维能力矩阵 + 按表数分级回退 | 局部探索规则 + CD-C `DPHyp` 超图枚举 |
| **CBO 物理后处理** | `Programs.sequence` + 二阶段 `Calc` | `physicalRuleRewrite` + `dynamicRewrite` | `PlanPostProcessors` |
| **跨查询历史反馈** | 开放 `RelMetadataProvider` 供外部注入 | `PlanTuningAdvisor` 调优指南缓存 | `HboPlanStatisticsProvider` 历史统计匹配 |

一句话总结框架与自研的取舍：**Calcite 用多层抽象税换取了跨生态多 Convention 复用的上限；StarRocks 与 Doris 非常清楚自家执行器怎么跑 Hash Shuffle、怎么管内存和列存分桶，因而用自研建设成本换取了从优化器物理属性直通列存 `Segment` 与向量化算子的极致垂直协同。**

### 8.2 排障七步证据链与 Calcite 集成四条避坑铁律

在实际研发与线上慢 SQL 调优中，无论面对哪一套 CBO，最忌讳的就是“一看到烂计划就去改代价公式系数”。请按以下七步证据链顺藤摸瓜，并时刻牢记两条工程底线——**统计未知（Unknown）绝不等于零成本（Zero）**，**搜索超时截断（Timeout）绝不等于证明了全局最优**：

1. **看合法候选到底生成了没有？**（借助 Calcite [`RuleMatchVisualizer`](https://calcite.apache.org/javadocAggregate/org/apache/calcite/plan/visualizer/RuleMatchVisualizer.html) / `RelOptListener` 或引擎 `TRACE LOGS OPTIMIZER` 确认外连接是否成功消除、重排规则是否因表数超限被熔断）；
2. **看目标物理属性（Trait / Property）是否可达？**（是否缺了对应的 Converter 或 Enforcer 导致整条分支无法满足根节点要求）；
3. **抓基数估算第一次偏离数量级的算子**（从底表 `Scan` NDV 到 `Filter` 联合选择率再到 `Join` 输出行数逐层核对，检查是否因缺统计回退到了荒谬默认值）；
4. **拆解胜出与落选候选的 Cost 分项**（究竟是内存惩罚、网络洗牌还是 CPU 权重主导了翻盘）；
5. **检查搜索预算闸门**（DP 表 `bloat` 上限、规则匹配次数与超时阈值是否提前砍掉了好计划）；
6. **比对出 Memo 瞬间的物理树与最终 Fragment**（确认物理后处理有没有意外破坏分布或可空性契约）；
7. **用 Positive / Negative / Fixpoint / Property 四维单测锁死规则边界**（特别是覆盖 `b.x IS NULL`、`coalesce(b.x, 0) > 1`、`ROW(a,b) IS DISTINCT FROM ROW(c,d)` 与 `NOT IN / NOT EXISTS` 等反例数据）。

如果你正准备在自己的数据系统里深度集成或改造 Calcite 的 `VolcanoPlanner`，还务必把下面四条避坑铁律写进团队的设计评审清单：

1. **想让 CPU、内存或网络字节真正决定计划选型，第一件事就是换掉 `VolcanoCost.FACTORY`**：实现自己的 `RelOptCost` 与 `RelOptCostFactory`，同时检查 `VolcanoPlanner.getCost` 里的 `!zeroCost.isLt(cost)` 零值判定，别让零行高算力算子被悄悄抹平成 `TINY`；
2. **启用多维代价比较前，先修好 `minus()` 减法下溢**：如果在 `topDownOpt = true` 下让 `isLe` 开始检查 CPU 或网络维度，必须保证 `minus()` 扣减上界时的下界估计自洽（或做非负截断保护），否则负 CPU 上界会误杀大批合法物理分支；
3. **打开 `topDownOpt = true` 不是改一行配置就完事**：你的自定义物理算子必须完整实现 `PhysicalNode` 接口（`passThroughTraits`、`deriveTraits` 与恰当的 `DeriveMode`），否则父节点的排序或分布需求根本传不下去；
4. **在自定义 `Convention` 里老老实实写好 `enforce()`**：这是自顶向下模式生成 `Sort` 与 `Exchange` 补偿算子的唯一正规渠道，同时配齐 `canConvertConvention` 与 `useAbstractConvertersForConversion` 的返回契约，才能彻底告别难以调试的 `CannotPlanException`。

### 8.3 关键源码索引

| 核心主题 | 所属项目 | 关键源码路径 |
| --- | --- | --- |
| Hep 与 Volcano 双引擎核心 | Apache Calcite | [`HepPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepPlanner.java)、[`HepProgramBuilder.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/hep/HepProgramBuilder.java)、[`VolcanoPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java)、[`Programs.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/tools/Programs.java) |
| 子集账本、默认代价与下界元数据 | Apache Calcite | [`RelSubset.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)、[`RelSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSet.java)、[`VolcanoCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoCost.java)、[`RelMetadataQuery.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMetadataQuery.java)、[`RelMdLowerBoundCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdLowerBoundCost.java) |
| 两种 RuleDriver 与开关配置 | Apache Calcite | [`IterativeRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleDriver.java)、[`IterativeRuleQueue.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleQueue.java)、[`TopDownRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleDriver.java)、[`TopDownRuleQueue.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleQueue.java)、[`CalciteSystemProperty.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteSystemProperty.java)、[`CalciteConnectionProperty.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteConnectionProperty.java) |
| Trait 补偿、Converter 与物理节点传导 | Apache Calcite | [`RelTraitSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelTraitSet.java)、[`RelCollationTraitDef.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/RelCollationTraitDef.java)、[`AbstractConverter.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/AbstractConverter.java)、[`CollationConversionTest.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/test/java/org/apache/calcite/plan/volcano/CollationConversionTest.java)、[`PhysicalNode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/PhysicalNode.java)、[`DeriveMode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/DeriveMode.java)、[`EnumerableConvention.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableConvention.java)、[`EnumerableMergeJoin.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoin.java)、[`EnumerableMergeJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoinRule.java) |
| Null-Rejecting 与谓词传递推导 | Apache Calcite | [`Strong.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/Strong.java)、[`FilterJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/FilterJoinRule.java)、[`RelOptUtil.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelOptUtil.java)、[`RelMdPredicates.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java)、[`JoinPushTransitivePredicatesRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinPushTransitivePredicatesRule.java)、[`JoinDeriveIsNotNullFilterRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinDeriveIsNotNullFilterRule.java) |
| MultiJoin 启发式与超图 DPHyp 重排 | Apache Calcite | [`JoinToMultiJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToMultiJoinRule.java)、[`LoptOptimizeJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/LoptOptimizeJoinRule.java)、[`JoinToHyperGraphRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/JoinToHyperGraphRule.java)、[`ConflictDetectionHelper.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/ConflictDetectionHelper.java)、[`DpHyp.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/rules/DpHyp.java) |
| 优化总控、外连接收紧、重排与反馈 | StarRocks | [`QueryOptimizer.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java)、[`JoinPredicatePushdown.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rewrite/JoinPredicatePushdown.java)、[`JoinReorderProperty.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/join/JoinReorderProperty.java)、[`PlanTuningAdvisor.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/feedback/PlanTuningAdvisor.java) |
| Nereids 空值折叠、CD-C 超图、后处理与 HBO | Apache Doris | [`NereidsPlanner.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/NereidsPlanner.java)、[`ExpressionUtils.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/util/ExpressionUtils.java)、[`EliminateOuterJoin.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/rules/rewrite/EliminateOuterJoin.java)、[`ConflictRulesMaker.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/hypergraph/ConflictRulesMaker.java)、[`JoinOrderJob.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/jobs/joinorder/JoinOrderJob.java)、[`PlanPostProcessors.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/PlanPostProcessors.java)、[`HboPlanStatisticsProvider.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboPlanStatisticsProvider.java)、[`HboStatsCalculator.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/stats/HboStatsCalculator.java) |

---

> **源码基线说明**：文中的源码链接及第七节实跑输出的物理计划、代价与 410 条任务 Trace 均基于 `apache/calcite` 提交 `e8e0dd5`（2026-05-04，介于 1.41.0 与 [1.42.0 正式版](https://calcite.apache.org/docs/history.html#1420--2026-05-31)之间，采用 `Programs.standard()` 阶段编排配合默认 Enumerable 规则集编译运行）、`StarRocks/starrocks` 提交 `0fd27fd`（2026-03-30）及 `apache/doris` 提交 `5202d06`（2026-08-21）。2026-10 对照上游开发线快照 Calcite `68f1613`（1.43.0 开发线）、StarRocks `f977b53` 与 Doris `7ac0eb2f` 完成全量复核：`RelSubset` 账本字段、`VolcanoCost` 的 `if (true)` 单维比较、`IterativeRuleDriver`（84 行）、`RelMdLowerBoundCost`、Trait 补偿协议及核心框架分工均未变化；期间上游的关键修正与演进（Calcite 修复多字段 `RelOptUtil.isDistinctFrom()` 的 `OR/AND` 组合逻辑及 `RelMdPredicates` 右外连接推导归类、新增 `EmptyRowTypePolicy` / `checkEmptyRowType`、`TopDownRuleDriver` 栈容器由 `Stack` 改为 `ArrayDeque`、`EnumerableMergeJoin` 布尔键显式标注 `Function1.class`；StarRocks 在 `GlobalLateMaterializationRewriter` 前置入 `WidenProjectionNullableRule`；Doris 将 `inferNotNull` 按过滤谓词与忽略空值聚合参数分拆、放宽 `OuterJoin*Project` 规范化匹配并在 `RuntimeFilterPruner` 保留分区/分桶裁剪过滤器）均已同步融入正文。

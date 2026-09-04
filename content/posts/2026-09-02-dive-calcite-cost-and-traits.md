---
title: "【源码】深入 Calcite VolcanoPlanner：Cost 账本、两种 RuleDriver 与 Trait Enforcement"
date: 2026-09-02T00:00:00+08:00
lastmod: 2026-09-02T00:00:00+08:00
slug: "dive-calcite-cost-and-traits"
categories:
  - 数据库
tags:
  - Apache Calcite
  - Cascades Optimizer
  - 查询优化器
  - CBO
  - Trait
  - Enforcer
description: "结合 Apache Calcite 源码，分析 VolcanoPlanner 如何记账、比较与传播代价，IterativeRuleDriver 与 TopDownRuleDriver 在剪枝上的本质差异，Trait 如何通过 AbstractConverter 或 Convention.enforce 被强制满足，并用一个真实运行的 Join + ORDER BY 例子走读完整任务时序。"
draft: false
---

上一篇《[深入 Calcite 优化器：Hep、Volcano、谓词推导与 Join Reorder]({{< relref "2026-08-24-dive-calcite.md" >}})》回答的是"Calcite 的能力边界在哪里"：Hep 与 Volcano 各自解决什么问题、Metadata 如何充当事实总线、Null-Rejecting 与 Outer Join Reorder 的合法性来自哪里、CBO 之后为什么还要一轮物理改写。那篇文章把 Volcano 当作一个"会按代价搜索的黑盒"来使用，`RelSet` / `RelSubset` 只给了一张示意图。

本文接着把这个黑盒拆开，只回答三个问题：

1. **代价究竟记在哪里、怎么比较、怎么传播？** 谁是"最优"的裁判，裁判在哪一刻落锤？
2. **Trait 不满足时，谁负责补上 Enforcer？** 补的是真实物理算子，还是占位符？
3. **搜索过程真的在剪枝吗？** Cascades 教科书里的 Upper/Lower Bound branch-and-bound，在 Calcite 的默认路径上到底有没有跑起来？

这三个问题的答案有相当一部分与"Calcite 是标准 Cascades 实现"这一直觉不符，所以本文全部结论都对应到具体源码行，并附一个在本机真实跑通、可复现的例子。

## 核心结论

1. **`VolcanoCost` 的比较只看 `rowCount`。** `isLe` / `isLt` 内部都有一个 `if (true)` 短路分支，真正比较三元组 `(rowCount, cpu, io)` 的代码是其后的死代码。cpu 与 io 会被完整累加、完整打印，但从不参与任何一次胜负判定。
2. **默认路径（`IterativeRuleDriver`）完全不剪枝。** `calcite.planner.topdown.opt` 默认为 `false`，此时 `drive()` 就是一个"取匹配—执行—canonize"的裸循环，`IterativeRuleQueue` 明确注释"The rules are not sorted in any way"。历史上的 importance 启发式已经被移除，代价在整个搜索期间**不做任何裁决**，只在最后 `buildCheapestPlan()` 时一次性生效。
3. **真正的 Cascades 上下界剪枝只存在于 opt-in 的 `TopDownRuleDriver` 里。** 它有 11 类任务、显式任务栈、逐孩子收紧的 Upper Bound；但即使打开，`RelMdLowerBoundCost` 对逻辑节点直接返回 `null`，所以 Lower Bound 只有在物理候选已经出现后才有意义。
4. **Enforcer 的形态由 driver 在唯一一处调用点决定：`RelSet.addConverters(subset, required, !planner.topDownOpt)`。** 自底向上时插入代价为**无穷大**的 `AbstractConverter` 占位符（后续由 `ExpandConversionRule` 展开）；自顶向下时直接调 `Convention.enforce()` 生成真实物理算子（`EnumerableConvention` 会包一个 `EnumerableSort`）。
5. **Calcite 没有 StarRocks 那样的 Required/Output/Guarantor Property Deriver 三件套。** 等价职责分散在 `PhysicalNode.passThrough*`（父需求下压）、`derive*`（孩子属性上拉）与 `RelSubset` 上的 DELIVERED / REQUIRED 两个 bit；`disableEnforcing()` 用来关掉"自己推出来的需求又对自己强制一遍"这种无意义 Enforcer。
6. **Upper Bound 的减法在非 rowCount 维度上会算出负数，而且没人报错。** 真实 trace 里出现了 `upperBound={30044.0 rows, -29527.48 cpu, 0.0 io}`。这不是 bug 触发，而是结论 1 的直接推论：既然比较只看 rowCount，其余维度算成负数也不会影响任何判定。

### 源码分析基线

| 项目 | 源码快照 | 日期 | 重点范围 |
|------|----------|------|----------|
| Apache Calcite | [e8e0dd5](https://github.com/apache/calcite/tree/e8e0dd54145c44f61b73acad1ffb96c14bddff78) | 2026-05-04 | VolcanoPlanner、RelSet/RelSubset、VolcanoCost、IterativeRuleDriver、TopDownRuleDriver、Trait Enforcement |

与上一篇同一提交（1.41.0 之后、1.42.0 发布之前），因此两篇文章的源码引用互相可比。

本文第八节的例子是在这个快照上**实际编译运行**得到的，计划、代价数字与任务 trace 都是程序真实输出，不是手工推演。但要强调：Rule 注册集合、Program 编排、Adapter 提供了哪些 Metadata，最终都由集成方决定；本文描述的是"用 `Programs.standard()` + Enumerable Convention 跑这个快照"时的行为。

## 一、`findBestExp()` 的生命周期

比上一篇的示意图更贴近源码的一条线是这样的：

```text
setRoot(rel)
  │  registerImpl(rel, null) 递归注册整棵树
  │    ├─ RelDigest 去重（mapDigestToRel）
  │    ├─ 建 RelSet（逻辑等价类）
  │    └─ 建 RelSubset（RelSet × RelTraitSet）
  ▼
findBestExp()
  │
  ├─① ensureRootConverters()
  │     为与 root traitSet 只差 1 个 trait 的 subset 注册 AbstractConverter
  │     注意：这一步与 topDownOpt 无关，两种 driver 都会插
  │
  ├─② registerMaterializations()   物化视图候选入 Memo
  │
  ├─③ ruleDriver.drive()   ★ 唯一的搜索循环
  │     ├─ IterativeRuleDriver：裸 while，无剪枝           （默认）
  │     └─ TopDownRuleDriver：Cascades 任务栈 + branch&bound（opt-in）
  │
  ├─④ dumpRuleAttemptsInfo()
  │
  └─⑤ root.buildCheapestPlan(this)  ★ 代价真正落锤的地方
        CheapestPlanReplacer 沿 subset.best 链条把 Memo 还原成物理树
```

只有 ③ 和 ⑤ 与本文主题直接相关，而它们的分工是全文的关键：**③ 负责把候选塞满 Memo，⑤ 负责按 `best` 指针抽计划**。在默认 driver 下，③ 里没有任何"因为太贵所以不搜了"的判断。

`ensureRootConverters()` 有一个容易看错的细节：

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

`!topDownOpt` 只影响**去重集合的预填充**，第二个循环无条件执行。所以打开 topDownOpt 之后，根节点上依然会出现 `AbstractConverter` —— 第八节的真实 trace 第 10 行就是它。

## 二、Memo 的代价账本：`RelSubset` 上到底记了什么

`RelSet` 是逻辑等价类，`RelSubset` 是 `RelSet × RelTraitSet`。所有代价状态都挂在 `RelSubset` 上：

| 字段 | 类型 | 语义 | 谁在写 |
|---|---|---|---|
| `best` | `@Nullable RelNode` | 当前该 subset 下最便宜的物理表达式 | `propagateCostImprovements` |
| `bestCost` | `RelOptCost` | `best` 的**累计**代价，初值 `infCost` | 同上 |
| `upperBound` | `RelOptCost` | 搜索上界，初值 `infCost` | `TopDownRuleDriver` |
| `taskState` | `@Nullable OptimizeState` | `null` / `OPTIMIZING` / `COMPLETED` | `TopDownRuleDriver` |
| `state` | `int` 位掩码 | `1=DELIVERED`、`2=REQUIRED` | `setDelivered` / `setRequired` |
| `enforceDisabled` | `boolean` | 是否跳过为该 subset 生成 Enforcer | `disableEnforcing()` |
| `timestamp` | `int` | 代价变更计数，用于 Metadata 缓存失效 | `propagateCostImprovements` |

其中 `upperBound` / `taskState` / `state` / `enforceDisabled` **只在 top-down 路径下有实际读者**。默认 driver 跑完之后，`taskState` 一直是 `null`，`upperBound` 一直是 `infCost`。这正是结论 2 与 3 的结构性原因：账本上留好了剪枝用的字段，但默认没人去读。

### 2.1 累计代价 vs 自身代价

两个概念在源码里是分开的：

- `RelMetadataQuery.getNonCumulativeCost(rel)` —— 算子自身代价，由各 `computeSelfCost` 提供；
- `VolcanoPlanner.getCost(rel, mq)` —— 累计代价，递归求和。

```java
@Override public @Nullable RelOptCost getCost(RelNode rel, RelMetadataQuery mq) {
  if (rel instanceof RelSubset) {
    return ((RelSubset) rel).bestCost;          // ① 遇到 subset 直接取账本
  }
  if (noneConventionHasInfiniteCost
      && rel.getTraitSet().getTrait(ConventionTraitDef.INSTANCE) == Convention.NONE) {
    return costFactory.makeInfiniteCost();      // ② 逻辑节点视为无穷贵
  }
  RelOptCost cost = mq.getNonCumulativeCost(rel);
  if (cost == null) {
    return null;
  }
  if (!zeroCost.isLt(cost)) {
    // cost must be positive, so nudge it
    cost = costFactory.makeTinyCost();          // ③ 非正代价抬到 TINY
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

三处细节都值得单独记住：

- **①** 递归在 `RelSubset` 处终止，取的是 `bestCost` 快照。这意味着累计代价是**账本的函数**，账本没更新时上层代价也不会变——所以才需要 `propagateCostImprovements` 主动推送。
- **②** 逻辑 Convention 的节点代价为无穷。这是 Volcano 逼迫搜索走到物理形态的机制，也是逻辑节点无法给出有意义 Lower Bound 的根源（见第七节）。
- **③** `zeroCost.isLt(cost)` 用的还是 rowCount-only 比较。所以一个 `(0 rows, 1e9 cpu, 0 io)` 的自身代价会被判定为"非正"并被抬成 `TINY = (1, 1, 0)`，cpu 那 `1e9` 直接消失。

### 2.2 代价改善如何向上传播

`propagateCostImprovements` 是一个以代价为优先级的堆遍历，方向自下而上：

```java
propagateRels.put(rel, getCostOrInfinite(rel, mq));
propagateHeap.offer(rel);

while ((relNode = propagateHeap.poll()) != null) {
  RelOptCost cost = requireNonNull(propagateRels.get(relNode), ...);
  for (RelSubset subset : getSubsetNonNull(relNode).set.subsets) {
    if (!relNode.getTraitSet().satisfies(subset.getTraitSet())) {
      continue;                                  // trait 不满足，不能当这个 subset 的 best
    }
    if (relNode != subset.best && !cost.isLt(subset.bestCost)) {
      continue;                                  // 不更便宜，跳过
    }
    if (relNode == subset.best && cost.equals(subset.bestCost)) {
      continue;
    }
    subset.timestamp++;
    subset.bestCost = cost;
    subset.best = relNode;
    mq.clearCache(subset);                        // 账本变了，Metadata 缓存必须失效
    for (RelNode parent : subset.getParents()) {
      mq.clearCache(parent);
      RelOptCost newCost = getCostOrInfinite(parent, mq);
      ...                                         // 更便宜则入堆，继续向上
    }
  }
}
```

三个要点：

1. **一个物理表达式会被尝试写进 `RelSet` 里所有它能满足的 subset。** `relNode.getTraitSet().satisfies(subset.getTraitSet())` 就是 trait 侧的准入条件——它同时也是"为什么一个 `EnumerableMergeJoin.[[0],[3]]` 能同时充当 `.[0]` subset 的 best"的解释。
2. **注释里坦白承认代价可能变大。** 源码原文提到 `JdbcAdapterTest#testVolcanoPlannerInternalValid` 观察到代价上升的情形，因此 `relNode == subset.best` 时无条件更新而非只接受下降。所以 `bestCost` 不是严格单调递减的。
3. **`mq.clearCache` 出现两次**，subset 与每个 parent 各一次。Metadata 缓存与代价账本的一致性靠这里手工维护。

### 2.3 落锤时刻：`buildCheapestPlan`

```java
RelNode buildCheapestPlan(VolcanoPlanner planner) {
  CheapestPlanReplacer replacer = new CheapestPlanReplacer(planner);
  final RelNode cheapest = replacer.visit(this, -1, null);
  ...
  return cheapest;
}
```

`CheapestPlanReplacer` 从 root subset 出发，每遇到 `RelSubset` 就换成它的 `best`，递归到底。整个"选最优计划"的动作就是**沿 `best` 指针走一遍**——没有任何搜索、没有任何比较。

如果某个 subset 的 `best` 是 `null`，就走到 `DeadEndFinder` + `traitDiff()`，产出那条大家都很熟的 `CannotPlanException`（"There are not enough rules to produce a node with desired properties"）。换个视角看，这条异常的真实含义是：**账本上有一格始终没被填上**。

## 三、代价模型：`VolcanoCost` 只比 rowCount

`VolcanoPlanner` 的默认代价工厂就是 `VolcanoCost.FACTORY`（`VolcanoPlanner:234`）：

```java
super(costFactory == null ? VolcanoCost.FACTORY : costFactory, ...);
```

它持有 `(rowCount, cpu, io)` 三元组，`plus` / `minus` / `multiplyBy` 都是逐维运算。但比较不是：

```java
@Override public boolean isLe(RelOptCost other) {
  VolcanoCost that = (VolcanoCost) other;
  if (true) {
    return this == that || this.rowCount <= that.rowCount;
  }
  return (this == that)
      || (this.rowCount <= that.rowCount
          && this.cpu <= that.cpu
          && this.io <= that.io);      // 死代码
}

@Override public boolean isLt(RelOptCost other) {
  if (true) {
    VolcanoCost that = (VolcanoCost) other;
    return this.rowCount < that.rowCount;
  }
  return isLe(other) && !equals(other);   // 死代码
}
```

`if (true)` 是源码里真实存在的写法，不是我为了说明而简化的。它的后果贯穿全文：

- **cpu / io 是"会计科目"而不是"裁判依据"。** 它们被 `plus` 累加、被 `RelWriter` 打印进 `cumulative cost = {... rows, ... cpu, ... io}`，看起来很详细，但任何两个计划的胜负只由 rowCount 决定。
- **rowCount 相同即为平局，胜负由搜索顺序决定。** 第八节的例子正好命中这一点：两种 driver 选出的 Merge Join 左右孩子完全相反，而累计代价一字不差。
- **`minus` 在非 rowCount 维度上算出负数不会被发现。** 结论 6 的那条 trace 就是这样产生的。
- **想要真正的多维代价，必须自带 `RelOptCostFactory`。** 这也是 Flink、Hive 等集成方普遍自定义代价的原因之一——不只是权重不合适，而是默认实现根本不比较除 rowCount 之外的维度。

几个常量也值得记住：`ZERO = (0,0,0)`、`TINY = (1,1,0)`、`HUGE`、`INFINITY`。`AbstractConverter.computeSelfCost` 返回 `makeInfiniteCost()`，这是第五节的关键。

## 四、两种 RuleDriver：默认路径不剪枝

driver 在 `initRuleQueue()` 里二选一，开关是 `VolcanoPlanner.topDownOpt`：

```text
CalciteSystemProperty.TOPDOWN_OPT
  = booleanProperty("calcite.planner.topdown.opt", false)   ★ 默认 false
        │
        ├─ CalciteConnectionProperty.TOPDOWN_OPT("topDownOpt", ...)
        │     └─ CalcitePrepareImpl:459
        │           planner.setTopDownOpt(prepareContext.config().topDownOpt())
        │
        └─ PlannerImpl:184
              new VolcanoPlanner(costFactory, context)   ← 没有 setTopDownOpt
                （只能靠系统属性生效）
```

也就是说：走 JDBC 时可以用连接参数 `topDownOpt=true` 打开；走 `Frameworks` / `PlannerImpl` 时只能靠 `-Dcalcite.planner.topdown.opt=true`。两条路的默认值都是 `false`。

### 4.1 `IterativeRuleDriver`：84 行，没有剪枝

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

@Override public void onProduce(RelNode rel, RelSubset subset) { }
@Override public void onSetMerged(RelSet set) { }
```

值得注意的地方按重要性排列：

1. **循环条件里没有代价。** `planner.root.bestCost` 只被打进 debug 日志，从不参与 `break` 判断。唯一的提前退出是 `VolcanoTimeoutException`。
2. **两个回调都是空实现。** `onProduce` / `onSetMerged` 是 driver 接口留给增量维护的钩子，iterative 路径完全不需要。
3. **队列是 FIFO。** `IterativeRuleQueue` 的 `MatchList` 只分 `preQueue`（`SubstitutionRule`）与 `queue`（其余），注释直说 "The rules are not sorted in any way"。早期 Calcite 的 importance 启发式（按 subset 重要性排序规则匹配）已不复存在。

结论：默认路径是**穷举式的自底向上枚举**。它靠 `RelDigest` 去重和 `RuleQueue.skipMatch()`（prunedNodes + 重复 subset 环检测）控制规模，靠 `Convention.NONE` 无穷代价保证最终选出物理计划，但它不会因为"这个分支已经比现有最优贵"而放弃搜索。

### 4.2 `TopDownRuleDriver`：11 类任务的 Cascades 栈

977 行，是 volcano 包里最值得逐行读的文件。`drive()` 从压入根任务开始：

```java
OptimizeGroup(planner.root, planner.infCost)
```

任务类型与职责：

| 任务 | 职责 |
|---|---|
| `OptimizeGroup` | 优化一个 subset；已有 winner 直接返回 |
| `GroupOptimized` | 标记 `taskState = COMPLETED` |
| `OptimizeMExpr` | 优化单个物理/逻辑表达式，分 explore / implement 两种模式 |
| `ExploreInput` | 只对孩子做逻辑变换（不 implement） |
| `EnsureGroupExplored` | 保证某个 input group 已被探索完 |
| `ApplyRules` | 批量触发规则，explore 时过滤为 transformation rule |
| `ApplyRule` | 执行单条 `VolcanoRuleMatch` |
| `OptimizeInput1` | 单孩子算子的孩子优化 |
| `OptimizeInputs` | 多孩子算子，**有状态**，逐孩子收紧上界 |
| `CheckInput` | 检查某个孩子是否拿到 winner，收紧或放弃 |
| `DeriveTrait` | 按 `DeriveMode` 从孩子已交付 trait 上拉 |

两个设计细节：

**Enforcer 被降优先级。** `OptimizeGroup.perform()` 里：

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
// Always apply O_INPUTS first so as to get a valid upper bound.
for (RelNode rel : physicals) {
  ...
}
```

`add(0, rel)` 看起来像是提高优先级，实际相反：`physicals` 随后被顺序压入 `tasks` 栈，索引 0 的元素**最先入栈、最后出栈**。这与 `TopDownRuleQueue` 是同一种"写法与语义相反"的栈序技巧。

降优先级的动机就在第二条注释里——先优化真实物理算子的孩子，才能拿到一个有限上界；若让无穷代价的 Enforcer 先跑，上界永远是 `inf`，剪枝无从下手。

**规则栈序被刻意反转。** `TopDownRuleQueue` 为每个 `RelNode` 维护一个 `Deque`：

```java
if (!planner.isSubstituteRule(match)) {
  queue.addFirst(match);
} else {
  queue.addLast(match);
}
```

因为消费端按栈弹出，`addLast` 的 substitution rule 反而**先**执行。这是"先做等价替换再做实现选择"的语义，写法上却是反的。

`ApplyRules` 的规则过滤同样体现两阶段：

```java
exploring ? planner::isTransformationRule : m -> true
```

explore 阶段只跑变换规则，implement 阶段放开全部。

## 五、Trait Enforcement：满足、交付、需求与两种 Enforcer

### 5.1 `satisfies` 是逐位比较，长度取 min

```java
public boolean satisfies(RelTraitSet that) {
  if (this == that) {
    return true;
  }
  final int n = Math.min(this.size(), that.size());
  for (int i = 0; i < n; i++) {
    if (!this.traits[i].satisfies(that.traits[i])) {
      return false;
    }
  }
  return true;
}
```

`Math.min` 这一步含义很强：**较短的 traitSet 里没提到的维度视为自动满足**。配套的 `simplify()` 会把 `RelCompositeTrait` 折叠成单 trait（只剩一个候选时），所以比较之前通常要先 simplify，否则复合 trait 与单 trait 之间的 `satisfies` 结果会不符合直觉。

对排序而言，`RelCollationTraitDef` 的三个属性决定了行为：`multiple()` 返回 `true`（同一节点可携带多个 collation）、`getDefault()` 是 `RelCollations.EMPTY`、`canConvert()` 恒为 `true`（任何 collation 都可以通过加 Sort 达成）。开关 `calcite.enable.collation.trait` 默认 `true`。

### 5.2 DELIVERED / REQUIRED：一个 subset 的两种身份

`RelSubset` 用两个 bit 区分"我能提供这个 trait"与"有人要求这个 trait"：

```java
private int state = 0;                 // 0 逻辑, 1 DELIVERED, 2 REQUIRED, 3 both

void setDelivered() { triggerRule = !isDelivered(); state |= DELIVERED; }
void setRequired()  { triggerRule = false;          state |= REQUIRED; }
void disableEnforcing() { assert isDelivered(); enforceDisabled = true; }
```

`triggerRule` 的赋值差异是关键：**只有"新交付"会打开规则触发，"被需求"不会**。因为需求本身不产生新候选，只是给搜索定目标。

`disableEnforcing()` 解决的是一个具体的浪费：当 `DeriveTrait` 从孩子交付的 trait 推出一个新需求，而这个需求恰好落回孩子自己所在的 subset（`newInput == subset`）时，再为它生成 Enforcer 毫无意义——孩子已经天然满足。此时直接关掉。

### 5.3 唯一的分叉点：`RelSet.addConverters`

整个 Enforcer 形态选择只有一处调用点：

```java
if (needsConverter) {
  addConverters(subset, required, !planner.topDownOpt);
}
```

第三个参数叫 `useAbstractConverter`，值就是 `!topDownOpt`。方法体里：

```java
if (useAbstractConverter) {
  enforcer = new AbstractConverter(cluster, from, null, to.getTraitSet());
} else {
  enforcer = convention.enforce(from, to.getTraitSet());
}
if (enforcer != null) {
  cluster.getPlanner().register(enforcer, to);
}
```

于是两条路径的 Enforcer 完全不同：

| | 自底向上（默认） | 自顶向下（opt-in） |
|---|---|---|
| Enforcer | `AbstractConverter` | `Convention.enforce()` 的返回值 |
| 自身代价 | `makeInfiniteCost()` | 真实物理代价 |
| 是否可执行 | 否，纯占位符 | 是 |
| 谁把它变成真算子 | `ExpandConversionRule` | 已经是真算子 |

`AbstractConverter` 的两个方法解释了它的定位：

```java
@Override public @Nullable RelOptCost computeSelfCost(RelOptPlanner planner, RelMetadataQuery mq) {
  return planner.getCostFactory().makeInfiniteCost();
}
@Override public boolean isEnforcer() { return true; }
```

无穷代价意味着它**永远不可能被 `buildCheapestPlan` 选中**，所以必须被替换。替换者是它的内嵌规则：

```java
@Override public void onMatch(RelOptRuleCall call) {
  final VolcanoPlanner planner = (VolcanoPlanner) call.getPlanner();
  AbstractConverter converter = call.rel(0);
  final RelNode child = converter.getInput();
  RelNode converted = planner.changeTraitsUsingConverters(child, converter.traitSet);
  if (converted != null) {
    call.transformTo(converted);
  }
}
```

类注释还给出了一条自我约束："AbstractConverters can be messy, so they restrain themselves: they don't fire if the target subset already has an implementation (with less than infinite cost)."

对照 top-down 侧，`EnumerableConvention.enforce()` 一次做完 convention 转换与排序：

```java
@Override public RelNode enforce(final RelNode input, final RelTraitSet required) {
  RelNode rel = input;
  if (input.getConvention() != INSTANCE) {
    rel = ConventionTraitDef.INSTANCE.convert(
        input.getCluster().getPlanner(), input, INSTANCE, true);
    requireNonNull(rel, ...);
  }
  RelCollation collation = required.getCollation();
  if (collation != null && collation != RelCollations.EMPTY) {
    rel = EnumerableSort.create(rel, collation, null, null);
  }
  return rel;
}

@Override public boolean canConvertConvention(Convention toConvention) { return false; }
@Override public boolean useAbstractConvertersForConversion(RelTraitSet f, RelTraitSet t) { return true; }
```

排序 Enforcer 就是一个 `EnumerableSort`，直接带真实代价进入竞争——这才是 Cascades 语义下的 Enforcer。

## 六、Top-down 的属性传导：`passThrough` 与 `derive`

Calcite 里没有 `RequiredPropertyDeriver` / `OutputPropertyDeriver` / `ChildOutputPropertyGuarantor` 这样的集中式组件。等价能力放在 `PhysicalNode` 接口的两组方法上，由每个物理算子自己实现：

```text
父需求 ──▶ passThrough(required) / passThroughTraits(required)
             把父要求翻译成孩子要求（能满足则返回带 trait 的自己，否则 null）

孩子交付 ──▶ derive(childTraits, childId) / deriveTraits(...)
             从孩子已交付的 trait 推出自己能交付什么
```

`derive` 的搜索范围由 `DeriveMode` 控制：

| DeriveMode | 语义 |
|---|---|
| `LEFT_FIRST` | 只从左孩子推 |
| `RIGHT_FIRST` | 只从右孩子推 |
| `BOTH` | 左右各推一轮 |
| `OMAKASE` | 一次拿到所有孩子的 trait 组合自行决定 |
| `PROHIBITED` | 不参与 derive |

`EnumerableMergeJoin.getDeriveMode()` 返回 `BOTH`，这正是第八节里 `depts` 能被换到左侧的原因之一。

`EnumerableMergeJoin` 的两个方法很能说明这套机制的粒度。`passThroughTraits` 按父要求与左键的关系分三种情况处理（父要求恰好等于左键集合时原样下压并映射右侧 collation；是左键前缀子集时用 `extendCollation` 补齐；否则放弃）。`deriveTraits` 则做严格校验：

```java
if (colCount < keyCount || keyCount == 0) {
  return null;                      // 孩子 collation 不够长，或没有 join key
}
...                                 // 截断到 keyCount
if (!childCollationKeys.equals(keySet)) {
  return null;                      // 前 keyCount 列必须正好是 join key
}
```

`EnumerableMergeJoinRule` 则负责准入与 collation 构造：拒绝 `IS NOT DISTINCT FROM`、拒绝不支持的 join 类型与笛卡尔积，然后为每个输入按 join key 构造 ASCENDING / NULLS LAST 的 collation，用 `convert(ord.e, traits)` 提出需求，最后把 join 自身的 traitSet `.replace(collations)`。

`PhysicalNode` 的类注释本身就是一份"如何让自己的算子支持 top-down trait 传导"的六步清单，自定义 Convention 时值得逐条对照。

## 七、上下界剪枝闭环（仅 top-down 有效）

### 7.1 Winner 的严格定义

```java
// RelSubset
public @Nullable RelOptCost getWinnerCost() {
  if (taskState == OptimizeState.COMPLETED && bestCost.isLe(upperBound)) {
    return bestCost;
  }
  return null;
}
```

两个条件都必须成立：**任务已完成**，且 **`bestCost` 不超过上界**。所以"有 best"不等于"有 winner"。默认 driver 下 `taskState` 恒为 `null`，`getWinnerCost()` 恒返回 `null`——这也解释了为什么剪枝逻辑在默认路径上等于不存在。

### 7.2 Lower Bound 对逻辑节点直接返回 null

`RelMdLowerBoundCost` 全文很短：

```java
public @Nullable RelOptCost getLowerBoundCost(
    RelSubset subset, RelMetadataQuery mq, VolcanoPlanner planner) {
  if (planner.isLogical(subset)) {
    return null;                     // "currently only support physical"
  }
  return subset.getWinnerCost();
}

public @Nullable RelOptCost getLowerBoundCost(
    RelNode node, RelMetadataQuery mq, VolcanoPlanner planner) {
  if (planner.isLogical(node)) {
    return null;
  }
  RelOptCost selfCost = mq.getNonCumulativeCost(node);
  if (selfCost != null && selfCost.isInfinite()) {
    selfCost = null;                 // 无穷自身代价（如 AbstractConverter）不计入下界
  }
  for (RelNode input : node.getInputs()) {
    RelOptCost lb = mq.getLowerBoundCost(input, planner);
    if (lb != null) {
      selfCost = selfCost == null ? lb : selfCost.plus(lb);
    }
  }
  return selfCost;
}
```

`isLogical(rel)` 的判定是 `!(rel instanceof PhysicalNode) && rel.getConvention() != rootConvention`。含义：**在物理候选出现之前，下界一律不可用**。搜索早期没有任何剪枝能力，必须先把物理形态生出来。这是 Calcite 与"从一开始就能用统计下界剪枝"的理想 Cascades 之间最实际的差距。

### 7.3 逐孩子收紧上界

`VolcanoPlanner.upperBoundForInputs` 先扣掉父节点自身代价：

```java
protected RelOptCost upperBoundForInputs(RelNode mExpr, RelOptCost upperBound) {
  if (!upperBound.isInfinite()) {
    RelOptCost rootCost = mExpr.getCluster().getMetadataQuery().getNonCumulativeCost(mExpr);
    if (rootCost != null && !rootCost.isInfinite()) {
      return upperBound.minus(rootCost);
    }
  }
  return upperBound;
}
```

`OptimizeInputs` 再按源码里写明的公式分给当前孩子：

```java
// UB(one input) = UB(current subset) - Parent's NonCumulativeCost - LB(other inputs)
upper = upperForInput.minus(lowerBoundSum).plus(lowerBounds.get(processingChild));
```

`CheckInput` 负责收尾：孩子拿到 winner 就用真实代价替换它原先的下界估计（进一步收紧），孩子没有 winner 就把 `lowerBoundSum` 置为 `planner.infCost`，等价于**放弃这个物理表达式的所有兄弟孩子**，不再浪费搜索。

这套减法在 rowCount 上是正确的。在 cpu 上不是——第八节的 trace 会给出负数证据。

## 八、一个具体例子的完整执行流程

### 8.1 复现方式

Schema 用 `ReflectiveSchema` 包一个 POJO：`emps` 10000 行（`deptno = i % 10`），`depts` 10 行。SQL：

```sql
SELECT e.deptno, e.ename, d.dname
FROM emps e JOIN depts d ON e.deptno = d.deptno
ORDER BY e.deptno;
```

配置 `Frameworks.newConfigBuilder().programs(Programs.standard())`，目标 traitSet 为 `ENUMERABLE` + root collation。任务 trace 的开关是一个专用 logger：

```properties
# CalciteTrace.getPlannerTaskTracer()
log4j.logger.org.apache.calcite.plan.volcano.task=DEBUG
```

driver 用 `-Dcalcite.planner.topdown.opt=true|false` 切换。下面所有计划、代价和 trace 都是这个程序的真实输出。

### 8.2 逻辑计划（两种 driver 相同）

```text
LogicalSort(sort0=[$0], dir0=[ASC])
  LogicalProject(deptno=[$2], ename=[$1], dname=[$4])
    LogicalJoin(condition=[=($2, $3)], joinType=[inner])
      LogicalTableScan(table=[[hr, emps]])
      LogicalTableScan(table=[[hr, depts]])
```

### 8.3 默认 driver（`topDownOpt=false`）的物理计划

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

两点值得注意：

- **顶层没有 Sort。** `ORDER BY e.deptno` 被 Merge Join 输出的 collation 直接满足了，这是 trait 传导的收益，不是规则改写的结果。
- **两个输入各有一个 `EnumerableSort`。** 它们是 `EnumerableMergeJoinRule` 通过 `convert(ord.e, traits)` 提出 collation 需求后，由 Enforcer 机制补上的。

### 8.4 Top-down driver（`topDownOpt=true`）的物理计划

```text
EnumerableCalc(deptno=[$t3], ename=[$t2], dname=[$t1])
    : cumulative cost = {70030.0 rows, 2007540.5914138353 cpu, 0.0 io}
  EnumerableMergeJoin(condition=[=($0, $3)], joinType=[inner])
      : cumulative cost = {55030.0 rows, 1902540.5914138353 cpu, 0.0 io}
    EnumerableSort(sort0=[$0], dir0=[ASC])          ← depts 在左
        : cumulative cost = {20.0 rows, 471.5170185988092 cpu, 0.0 io}
      EnumerableTableScan(table=[[hr, depts]])
    EnumerableSort(sort0=[$1], dir0=[ASC])          ← emps 在右
        : cumulative cost = {30000.0 rows, 1902069.0743952366 cpu, 0.0 io}
      EnumerableCalc(ename=[$t1], deptno=[$t2])
        EnumerableTableScan(table=[[hr, emps]])
```

**左右孩子完全对调，累计代价一字不差**（`70030.0` / `55030.0`，cpu 也相同）。这是结论 1 最干净的实证：`JoinCommuteRule` 生出的两个方向 rowCount 相同，rowCount-only 比较判为平局，最终由搜索顺序决定谁被写进 `subset.best`。如果代价比较真的看 cpu 或 io，两种 driver 应当收敛到同一个方向。

### 8.5 Top-down 任务时序

这一轮共 **410 个 `Execute task`**。按阶段摘取关键行：

**阶段 ①：根组与根 Enforcer**

```text
Execute task: OptimizeGroup(group=rel#41:RelSubset#5.ENUMERABLE.[0], upperBound={inf})
Execute task: OptimizeInput1(mExpr=rel#42:AbstractConverter.ENUMERABLE.[0](
                 input=RelSubset#36,convention=ENUMERABLE,sort=[0]), upperBound={inf})
Execute task: OptimizeGroup(group=rel#36:RelSubset#5.NONE.[0], upperBound={inf})
Skip optimizing because of traits: rel#42:AbstractConverter.ENUMERABLE.[0](...)
```

第 2 行就是第一节提到的现象：**打开 topDownOpt 之后根节点依然有 `AbstractConverter`**，来自 `ensureRootConverters()` 那个无条件的第二重循环。第 4 行的 "Skip optimizing because of traits" 说明它被识别为 Enforcer 后跳过了常规优化路径。

**阶段 ②：explore（只跑变换规则）**

```text
Execute task: OptimizeMExpr(mExpr=rel#33:LogicalProject.NONE.[](...), explore=true)
Execute task: ExploreInput(group=rel#32:RelSubset#3.NONE.[])
Execute task: OptimizeMExpr(mExpr=rel#31:LogicalJoin.NONE.[](...), explore=true)
Execute task: ExploreInput(group=rel#29:RelSubset#1.NONE.[])
Execute task: OptimizeMExpr(mExpr=rel#10:LogicalTableScan.NONE.[](table=[hr, emps]), explore=true)
Execute task: ApplyRules(mExpr=rel#10:LogicalTableScan.NONE.[](...), exploring=true)
Execute task: EnsureGroupExplored(mExpr=rel#28:LogicalProject.NONE.[](...), i=0)
...
Execute task: ApplyRules(mExpr=rel#31:LogicalJoin.NONE.[](...), exploring=true)
```

自顶向下深入到叶子，再靠 `EnsureGroupExplored` 逐个孩子回收。这一阶段命中 `JoinConditionPushRule`、`JoinCommuteRule`、`ProjectMergeRule`、`JoinPushExpressionsRule`。

**阶段 ③：implement 与孩子优化**

```text
Execute task: OptimizeInputs(mExpr=rel#74:EnumerableHashJoin.ENUMERABLE.[](
                 left=RelSubset#72,right=RelSubset#73,condition==($0, $3),joinType=inner),
                 upperBound={inf}, processingChild=0)
...
Execute task: OptimizeInputs(mExpr=rel#74:EnumerableHashJoin..., upperBound={inf}, processingChild=1)
```

Hash Join 先被实现出来，两个孩子在 `upperBound={inf}` 下依次优化——这时还没有任何有限上界可用。

**阶段 ④：上界变为有限，剪枝开始生效**

```text
Execute task: OptimizeGroup(group=rel#87:RelSubset#2.ENUMERABLE.[0],
                 upperBound={20023.02585092994 rows, 30012.0 cpu, 0.0 io})
Execute task: OptimizeInputs(mExpr=rel#91:EnumerableMergeJoin.ENUMERABLE.[[0], [3]](
                 left=RelSubset#87,right=RelSubset#89,condition==($0, $3),joinType=inner),
                 upperBound={45033.02585092994 rows, 30012.0 cpu, 0.0 io}, processingChild=1)
Execute task: OptimizeGroup(group=rel#89:RelSubset#1.ENUMERABLE.[1],
                 upperBound={20003.02585092994 rows, 29540.48298140119 cpu, 0.0 io})
```

Merge Join 的第 0 号孩子解出 winner 后，第 1 号孩子拿到的上界从 `inf` 收紧为 `45033.02` rows —— 第 7.3 节那个公式的真实运行结果。

再往后就出现了结论 6 的证据：

```text
Execute task: OptimizeGroup(group=rel#98:RelSubset#3.ENUMERABLE.[2],
                 upperBound={30044.02585092994 rows, -29527.48298140119 cpu, 0.0 io})
Execute task: OptimizeGroup(group=rel#58:RelSubset#6.ENUMERABLE.[],
                 upperBound={30044.02585092994 rows, -4008165.7846168266 cpu, 0.0 io})
```

**cpu 分量已经是负数**（−29527、−4008165）。因为 `upperBound.minus(...)` 逐维相减，而 cpu 维的下界估计与实际扣减并不自洽。这没有触发任何断言或异常——`bestCost.isLe(upperBound)` 只看 rowCount，负 cpu 对判定毫无影响。把 `VolcanoCost` 换成一个真正比较三维的代价实现之前，这类数值是无害的；换之后，它就是一个必须先修的前提。

**阶段 ⑤：DeriveTrait 上拉与收尾**

```text
Execute task: DeriveTrait(mExpr=rel#95:EnumerableMergeJoin.ENUMERABLE.[0](...),
                 group=rel#96:RelSubset#6.ENUMERABLE.[0])
Execute task: DeriveTrait(mExpr=rel#102:EnumerableHashJoin.ENUMERABLE.[0](
                 left=RelSubset#87,right=RelSubset#73,...), group=rel#96:RelSubset#6.ENUMERABLE.[0])
Execute task: GroupOptimized(group=rel#96:RelSubset#6.ENUMERABLE.[0],
                 upperBound={45044.02585092994 rows, 30472.51701859881 cpu, 0.0 io})
...
Execute task: CheckInput(parent=rel#42:AbstractConverter.ENUMERABLE.[0](input=RelSubset#55,...), i=0)
Execute task: ApplyRule(match=rule [ExpandConversionRule] rels [#42], exploring=false)
...
Execute task: GroupOptimized(group=rel#41:RelSubset#5.ENUMERABLE.[0], upperBound={inf})
```

最后一行闭合根 subset，`drive()` 返回，`buildCheapestPlan()` 沿 `best` 链条抽出 8.4 的计划。注意 `ExpandConversionRule` 在 top-down 路径下**依然被触发了一次**——就是为了展开 `ensureRootConverters()` 插入的那个根 `AbstractConverter`。

### 8.6 这一轮触发的规则分布

```text
  8  EnumerableLimitRule
  7  EnumerableProjectRule(in:NONE,out:ENUMERABLE)
  4  ProjectMergeRule
  2  SortProjectTransposeRule
  2  JoinPushExpressionsRule
  2  JoinConditionPushRule
  2  JoinCommuteRule
  2  EnumerableTableScanRule(in:NONE,out:ENUMERABLE)
  2  EnumerableMergeJoinRule(in:NONE,out:ENUMERABLE)
  2  EnumerableJoinRule(in:NONE,out:ENUMERABLE)
  2  EnumerableInterpreterRule(in:BINDABLE,out:ENUMERABLE)
  2  BindableTableScanRule
  1  SortRemoveRule
  1  SortRemoveConstantKeysRule
  1  ExpandConversionRule
  1  EnumerableSortRule(in:NONE,out:ENUMERABLE)
```

410 个任务只对应 41 次 `ApplyRule`。绝大部分任务是调度与状态管理（`OptimizeGroup` / `OptimizeMExpr` / `ExploreInput` / `CheckInput` / `DeriveTrait`）。这个比例说明 top-down driver 的成本主要花在"维护搜索状态"上，换来的是剪枝能力与 trait 传导的精确性。

同一条 SQL 在默认 driver 下不产生任何任务日志——它没有任务概念。

### 8.7 关于 `ExpandConversionRule` 的最小验证

如果只想确认"`AbstractConverter` + `ExpandConversionRule` 如何把 collation 需求变成真实 Sort"，测试目录里有一个刻意极简的例子：`CollationConversionTest`。它显式 `planner.setTopDownOpt(false)`、注册 `ExpandConversionRule`，并让自定义的 `TestRelCollationTraitDef.convert()` 返回一个 `PhysicalSort`，最终断言：

```text
RootSingleRel(ENUMERABLE-like phys, ROOT_COLLATION)
  PhysicalSort
    LeafRel(LEAF_COLLATION)
```

这正是 5.3 节那张表左列的最小可运行形态。

## 九、与 StarRocks 的对照

把《[深入 StarRocks Optimizer：Enforcer、Property 级联与 Cost 选型]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}})》放在旁边，差异集中在四处：

| 维度 | Apache Calcite | StarRocks |
|---|---|---|
| 搜索子目标 | `RelSubset` = `RelSet × RelTraitSet` | `Group.lowestCostExpressions` 按 `PhysicalPropertySet` 分桶 |
| 代价比较 | `VolcanoCost` **只比 rowCount** | `realCost = 0.5·cpu + 2·mem + 1.5·net` |
| 剪枝 | 默认 driver 无剪枝；上下界仅 top-down 且逻辑节点无下界 | `EnforceAndCostTask` 内在的 Upper Bound 收紧 |
| Property 推导 | 分散在 `PhysicalNode.passThrough*` / `derive*` + DELIVERED/REQUIRED bit | 集中的 Required/Output Deriver + Guarantor 三件套 |
| Enforcer 代价 | 默认路径是无穷代价占位符，需二次展开 | Exchange/Sort 直接带真实代价入竞争 |

结构性的解释是**定位不同**。StarRocks 的优化器服务于一个已知的分布式执行器，分布属性是一等公民，Enforcer 就是 Exchange，代价公式可以针对自家执行器标定权重。Calcite 是要被 Flink、Hive、Druid 等各自不同的执行器复用的框架，它无法预设分布模型，也无法预设哪一维代价重要——于是把 trait 体系做得高度可扩展（`RelTraitDef` + `multiple()` + `RelCompositeTrait`），却把默认代价实现留成了一个近乎占位的 rowCount 比较。

这也给集成 Calcite 的人一份具体的清单：

1. **必须自带 `RelOptCostFactory`。** 否则 cpu / io 的所有建模工作都不会影响任何决策。
2. **明确决定是否打开 `topDownOpt`。** 打开才有 branch-and-bound 与精确 trait 传导，但要求物理算子实现 `PhysicalNode`（`passThrough*` / `derive*` / `getDeriveMode`），否则 top-down 的优势用不上。
3. **自定义 Convention 时实现好 `enforce()`。** 这是 top-down 路径下 Enforcer 的唯一来源；`useAbstractConvertersForConversion` 与 `canConvertConvention` 的返回值决定了搜索的形状。
4. **换代价实现前先审计 `minus` 的使用。** 8.5 节的负 cpu 说明现有上下界减法只在 rowCount 维上自洽。

最后一句总结：Calcite 的 `VolcanoPlanner` 提供了一个**完整的 Cascades 骨架和一套刻意留白的默认实现**。骨架部分（Memo、trait 体系、任务栈、上下界字段）足以支撑生产级优化器；留白部分（代价比较、driver 默认值、Property 推导的分散实现）需要集成方自己补齐。把它当作"开箱即用的 CBO"会失望，把它当作"可替换关键决策点的框架"才符合它的设计。

---

## 附：关键源码索引

| 主题 | 文件 |
|---|---|
| 搜索入口与代价递归 | [VolcanoPlanner.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) |
| 代价模型（rowCount-only 比较） | [VolcanoCost.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoCost.java) |
| 代价账本与状态位 | [RelSubset.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java) |
| Enforcer 分叉点 `addConverters` | [RelSet.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSet.java) |
| 默认 driver（无剪枝） | [IterativeRuleDriver.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleDriver.java)、[IterativeRuleQueue.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleQueue.java) |
| Cascades 任务栈 | [TopDownRuleDriver.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleDriver.java)、[TopDownRuleQueue.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleQueue.java) |
| 占位 Enforcer 与展开规则 | [AbstractConverter.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/AbstractConverter.java) |
| Trait 满足判定 | [RelTraitSet.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelTraitSet.java) |
| 排序 Trait 定义 | [RelCollationTraitDef.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/RelCollationTraitDef.java) |
| 属性传导契约 | [PhysicalNode.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/PhysicalNode.java)、[DeriveMode.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/DeriveMode.java) |
| Lower Bound Metadata | [RelMdLowerBoundCost.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdLowerBoundCost.java) |
| 真实 Enforcer 生成 | [EnumerableConvention.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableConvention.java) |
| Merge Join 的 passThrough/derive | [EnumerableMergeJoin.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoin.java)、[EnumerableMergeJoinRule.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoinRule.java) |
| 开关与默认值 | [CalciteSystemProperty.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteSystemProperty.java)、[CalciteConnectionProperty.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteConnectionProperty.java) |
| Enforcement 最小测试 | [CollationConversionTest.java](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/test/java/org/apache/calcite/plan/volcano/CollationConversionTest.java) |

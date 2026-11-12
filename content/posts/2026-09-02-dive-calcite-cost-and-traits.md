---
title: "【源码】深入 Calcite VolcanoPlanner：Cost 账本、两种 RuleDriver 与 Trait Enforcement"
date: 2026-09-02T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
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
description: "结合 Apache Calcite 源码，拆解 VolcanoPlanner 为何把 TraitSet 直接焊进 RelSubset、VolcanoCost 为何只比对行数、默认迭代驱动器与自顶向下任务栈在剪枝和 Enforcer 上的分水岭，并用真实运行的 Join + ORDER BY Trace 走读完整时序。"
draft: false
toc: true
math: true
---

> 本系列第 2/4 篇 · 优化器 / Cascades · [上一篇]({{< relref "2026-08-24-dive-calcite.md" >}}) · [下一篇]({{< relref "2026-09-01-dive-optd-cascades-optimizer.md" >}})

如果你先读过经典的 Volcano / Cascades 论文，再打开 Apache Calcite 的 [`VolcanoPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) 单步调试一条最普通的连接排序查询：

```sql
SELECT e.deptno, e.ename, d.dname
FROM emps e JOIN depts d ON e.deptno = d.deptno
ORDER BY e.deptno;
```

不出十分钟，你就会撞上三个颠覆教科书直觉的现象：

1. **物理属性（`TraitSet`）不是查找键，而是直接长成了 Memo 里的节点**：教科书里的 Memo `Group` 只代表逻辑等价类，物理属性只是组内 Winner 哈希表的 Key；而在 Calcite 里，逻辑等价类叫 `RelSet`，**`RelSubset = RelSet × RelTraitSet` 本身就是一个继承自 `RelNode` 的算子节点**，直接挂在父算子的输入槽位上！
2. **账本上留着剪枝字段，默认搜索引擎却一行都不看**：`RelSubset` 明明定义了 `upperBound`（代价上界）和 `taskState`（任务状态），但默认的 `IterativeRuleDriver` 只是一个 84 行的 `while (true)` 先进先出队列——跑完全程，`upperBound` 依然是无穷大、`taskState` 依然是 `null`，没有任何分支因为“太贵”被提前截断；可诡异的是，最后调 `buildCheapestPlan()` 提取最优计划时，代码里连一次代价大小比较都没有，顺着指针一摸就拿到了最便宜的物理树！
3. **打印得有鼻子有眼的三维代价，裁判其实只看第一列**：每次 `EXPLAIN` 都会工整地印出 `{rows, cpu, io}` 累计代价，但翻开默认代价实现 `VolcanoCost.java`，它的大小比较函数里赫然写着一行硬编码的 `if (true)`——**胜负完全由 `rowCount` 一锤定音，`cpu` 和 `io` 根本不参与排序！**

为什么一套被 Flink、Hive、Druid 等众多知名系统倚重的工业级优化器框架会长成这样？[上一篇 Calcite 总览]({{< relref "2026-08-24-dive-calcite.md" >}}) 我们梳理了 Hep 与 Volcano 的宏观分工，这一篇我们就顺着下面这张全景架构图，钻进 `RelSubset` 的代价账本、两种 `RuleDriver` 的调度分水岭以及 `AbstractConverter` 与 `Convention.enforce()` 的补偿博弈里，把每一个反直觉设计背后的工程逻辑彻底跑通。

![VolcanoPlanner 的账本、两种 Driver 与 Trait 补偿](/images/calcite-cost-traits.svg)

*图 1：从上到下依次为两套驱动器共用的 `RelSubset` 账本、默认的自底向上穷举路径（蓝）与可选的自顶向下分支限界路径（橙），以及底部的 Trait 补偿与指针提取机制。*

---

## 一、双层 Memo 与搜索入口：把 TraitSet 焊进 RelSubset 节点

要理解 `VolcanoPlanner` 的一切运转细节，第一步是看清它怎样把一棵初始逻辑计划塞进 Memo，又怎样在 `findBestExp()` 里走完搜索全流程。

当调用方执行 `setRoot(rel)` 时，规划器会递归调用 `registerImpl(rel, null)` 把整棵树拆解登记（在 1.43 开发线中，入口处还新增了一道 `checkEmptyRowType(rel)` 门禁，当上下文配置为 `EmptyRowTypePolicy.FORBIDDEN` 且遇到零列节点时直接拒绝注册）：

- 首先计算表达式摘要 `RelDigest` 在 `mapDigestToRel` 中做结构去重；
- 语义等价的所有算子归入同一个逻辑等价类 **`RelSet`**；
- 每个算子再根据自身携带的 `RelTraitSet`（例如执行约定 `Convention.NONE` vs `ENUMERABLE`、排序属性 `RelCollation` 等），落入该 `RelSet` 下对应的属性分桶 **`RelSubset`**，并把算子原本指向具体子节点的输入边全部替换成指向子 `RelSubset`！

看明白这一步设计了吗？在 Calcite 里，如果父算子 `MergeJoin` 要求左输入必须满足“`ENUMERABLE` 约定且按第 `0` 列升序”，它根本不需要维护额外的属性需求映射表——它只要把自己的左输入直接指向子 `RelSet` 里 trait 为 `ENUMERABLE.[0]` 的那个 `RelSubset` 实例就行了！哪怕此刻那个 `RelSubset` 里一个物理算子都还没有（空桶），引用关系也已经稳稳立住。

随后进入核心入口 `findBestExp()`，整个生命周期只有干净利落的五步：

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

这里立刻就能解开第一个谜团：为什么第 ⑤ 步提取计划时不做任何代价比较？因为**代价竞争早在第 ①～③ 步每一次新算子注册进 `RelSubset` 的瞬间，就已经实时结算并冒泡传播完毕了**；`ruleDriver.drive()` 只管催生新候选，账本自己会随时刷新擂主。

在进入账本细节前，先留心第 ① 步 `ensureRootConverters()` 里的一个经典源码陷阱：

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

很多人以为开启自顶向下模式（`topDownOpt = true`）后，系统就会彻底告别 `AbstractConverter`。仔细看上面的两个循环：`!topDownOpt` 只控制了第一个循环里去重集合 `subsets` 的预填充，**第二个注册循环是无条件执行的**！因此哪怕打开了 `topDownOpt`，只要根等价类里存在与目标属性相差 1 个维度的子集（例如逻辑排好序的 `NONE.[0]` 与目标的 `ENUMERABLE.[0]` 只差 Convention），根节点上照样会被塞进一个 `AbstractConverter`——后文真实 Trace 的第 2 行就会抓个正着。

---

## 二、`RelSubset` 代价账本与 `VolcanoCost` 的“单维裁判”陷阱

现在我们掀开 [`RelSubset.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)，看看这张被两条搜索路径共用的核心账本到底记了哪七笔账：

| 账本字段 | 类型与初值 | 真实工程语义 | 谁负责读写 |
| --- | --- | --- | --- |
| `best` | `@Nullable RelNode` | 当前能满足该 `RelSubset` 属性要求的最便宜物理算子 | `propagateCostImprovements` 写入，`buildCheapestPlan` 读取 |
| `bestCost` | `RelOptCost`（初值 `infCost`） | `best` 算子连同其全部子树的**累计总代价** | 同上 |
| `timestamp` | `int` | 每次 `best` 或 `bestCost` 变动时自增，用于触发 Metadata 缓存失效 | `propagateCostImprovements` |
| `upperBound` | `RelOptCost`（初值 `infCost`） | 分支限界搜索允许的成本天花板 | **仅 `TopDownRuleDriver` 读写** |
| `taskState` | `@Nullable OptimizeState` | 标记 `OPTIMIZING` 或 `COMPLETED` | **仅 `TopDownRuleDriver` 读写** |
| `state` | `int` 位掩码 | 区分属性是已由下层交付（`DELIVERED=1`）还是由上层索要（`REQUIRED=2`） | `setDelivered` / `setRequired`（服务 Top-Down） |
| `enforceDisabled` | `boolean` | 禁止对天然已满足属性的自身子集重复补插 Enforcer | `disableEnforcing()`（服务 Top-Down） |

表格下半部分的四列字段道破了天机：Calcite 在同一个 `RelSubset` 对象里兼容了两代搜索引擎。跑默认的 `IterativeRuleDriver` 时，后面四个字段全程沉睡，真正干活的只有上半部分的 **`best` + `bestCost` + 向上冒泡堆**。

### 2.1 局部代价、累计代价与自底向上的堆冒泡

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
- **关节 ③（隐蔽的 `TINY` 抹平效应）**：如果某个自定义算子返回了 `(0 rows, 1e9 cpu, 0 io)` 的自身代价，因为后面我们要讲的 `zeroCost.isLt(cost)` 只比对 `rowCount`，`0 < 0` 为假，这个算子的代价会被当成“非正数”强行替换成 `TINY = (1, 1, 0)`——那 `1e9` 的巨额 CPU 开销当场人间蒸发！

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

### 2.2 `VolcanoCost` 源码里的 `if (true)`：为什么 CPU 和 I/O 只是摆设？

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
2. 只要两条候选计划累计处理的总行数（`rowCount`）相等，裁判就判平局——谁先被搜索引擎枚举出来抢占了 `best`，谁就笑到最后（后文的真实运行示例就会撞上这一幕）；
3. `upperBound.minus(...)` 做上界扣减时是三个维度各自相减的，哪怕 `cpu` 维度减穿成了荒谬的负数，只要 `rowCount` 还是正的，比较器就浑然不觉；
4. 反过来，一旦你在自己的引擎里接管了多维代价比较，就必须先去审计 Top-Down 路径里的 `minus` 减法逻辑，否则负 CPU 分量会当场把剪枝逻辑搞崩。

---

## 三、两种搜索驱动器：84 行穷举队列与 11 类任务的分支限界栈

看懂了底层的共享账本，再向上看 `findBestExp()` 第 ③ 步的 `ruleDriver.drive()`，两条搜索路径的分野就一目了然了。切换开关是系统属性 `calcite.planner.topdown.opt`（默认 `false`；走 JDBC 连接串可通过 `topDownOpt=true` 触发 `CalcitePrepareImpl.setTopDownOpt`，而直接用 `Frameworks` / `PlannerImpl` 编程接入时没有透传该连接参数，只能靠 JVM 启动参数 `-Dcalcite.planner.topdown.opt=true` 或手动调 `planner.setTopDownOpt(true)` 开启）。

### 3.1 默认路径 `IterativeRuleDriver`：极简自底向上穷举

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

### 3.2 可选路径 `TopDownRuleDriver`：Cascades 任务栈与栈序反转技巧

当你把 `topDownOpt` 拨到 `true`，接管比赛的就是实现了完整 Cascades 协议的 [`TopDownRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleDriver.java)。它用一个后进先出栈（1.43 开发线已将过时的 `Stack<Task>` 替换为 `ArrayDeque<Task>`，队首 `push/pop` 顺序完全一致）驱动 11 类精细任务：

| 任务类别 | 包含的具体任务与分工 |
| --- | --- |
| **子集生命周期** | `OptimizeGroup`（入口检查已有 Winner，否则压入组内表达式任务）、`GroupOptimized`（收尾打上 `taskState = COMPLETED` 印章） |
| **逻辑空间探索** | `OptimizeMExpr`（区分 `explore` 仅做逻辑扩展与完整实现两模式）、`ExploreInput` + `EnsureGroupExplored`（深入并回收子组的逻辑变换） |
| **规则精准触发** | `ApplyRules`（探索期仅放行 `isTransformationRule`，实现期全放开）、`ApplyRule`（执行单次规则匹配） |
| **物理输入寻优与剪枝** | `OptimizeInput1`（单输入快路径）、`OptimizeInputs`（多输入有状态推进，逐个孩子收紧预算）、`CheckInput`（校验子 Winner，失败则连坐放弃兄弟分支） |
| **属性自底向上反哺** | `DeriveTrait`（当子节点交付了比要求更强的属性时，唤醒父物理算子推导新形态） |

读 `TopDownRuleDriver` 源码时，有两处**“代码字面写法与实际执行先后完全相反”**的压栈技巧千万别看反了。

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

看到 `physicals.add(0, rel)` 插到列表最前面，第一反应往往是“让 Enforcer 优先”。但别忘了紧接着有一个循环把 `physicals` 依次 `push` 进后进先出的 `tasks` 栈！排在索引 `0` 的 Enforcer 最先入栈被压在栈底，反而是排在后面的真实物理算子最后入栈、最先弹出执行！为什么要这么干？注释一语中的：**必须先跑真实物理算子尽快算出一个有限的 `upperBound`，如果让代价无穷大的占位 Enforcer 先跑，上界永远是 `inf`，剪枝根本启动不了。** 同样的栈反转技巧也出现在 `TopDownRuleQueue` 里：`SubstitutionRule` 被 `addLast` 放到双端队列尾部，只因弹出消费时从尾部先取，从而保证等价替换先于物理实现执行。

有了显式任务栈，上下界剪枝闭环才能真正运转起来：

1. **严苛的 Winner 认证（`RelSubset.getWinnerCost`）**：在 Top-Down 模式下，账本上有 `best` 绝不等于大功告成——必须同时满足 **`taskState == OptimizeState.COMPLETED` 且 `bestCost.isLe(upperBound)`**，才算拿到合法 Winner。
2. **仅对物理节点生效的下界估计（`RelMdLowerBoundCost`）**：剪枝不仅需要上界，还需要估算“还没优化的兄弟分支至少要花多少钱（Lower Bound）”。翻开 [`RelMdLowerBoundCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdLowerBoundCost.java) 可以看到，只要遇到逻辑节点（`planner.isLogical(node)`），它直接返回 `null`！也就是说，在物理候选真正浮出水面之前，Calcite 并不会用逻辑统计基数提前算下界剪枝；只有当物理算子登场后，它才累加算子自身非无穷代价与各输入的已知下界。
3. **逐个孩子收紧预算（`OptimizeInputs` + `CheckInput`）**：当多输入物理算子（如 Join）依次优化第 `i` 个孩子时，分给它的成本天花板由 `upperBoundForInputs` 扣掉父算子自身代价与其余兄弟孩子的下界之和给出：

$$\text{UB}(\text{child}_i) = \text{UB}(\text{subset}) - \text{SelfCost}(\text{parent}) - \sum_{j \neq i} \text{LB}(\text{child}_j)$$

一旦第 `0` 号孩子顺利拿回真实 Winner，`CheckInput` 立刻用这笔更紧的实付账单替换原先粗糙的下界，把第 `1` 号孩子的预算压得更低；反过来，要是第 `0` 号孩子在当前预算下空手而归（无解），`CheckInput` 直接把 `lowerBoundSum` 设为无穷大，当场赦免第 `1` 号孩子的无谓搜索！

---

## 四、Trait 补偿协议：`AbstractConverter` 占位符与 `PhysicalNode` 按需传导

在两种驱动器差异最大的地方，除了剪枝，就是**“当现有算子满足不了目标物理属性（Trait）时，怎么补救？”**

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

而一旦切到 `topDownOpt = true`，[`EnumerableConvention.enforce()`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableConvention.java) 直接一步到位：Convention 不对就调转换器，缺 `RelCollation` 排序就直接 `EnumerableSort.create(...)` 造出真算子扔进账本比价！

更进一步，Calcite 没有像 StarRocks 那样写一个包揽全局算子的中央 `RequiredPropertyDeriver`，而是把双向属性传导契约下放到了每个物理算子自己实现的 [`PhysicalNode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/PhysicalNode.java) 接口上：

- **自顶向下传需求（`passThroughTraits`）**：父节点带着排序要求敲门时，算子自己判断能不能接住并翻译成对输入子集的要求。比如 [`EnumerableMergeJoin.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoin.java) 检查父要求的排序列：如果恰好等于左连接键集合，就原样下压给左输入并映射成右连接键排序压给右输入；如果是左连接键的前缀子集，就用 `extendCollation` 补全连接键后下压；否则干脆返回 `null` 拒绝。
- **自底向上推能力（`deriveTraits` + `DeriveMode`）**：当某个子集产出了新的排序属性时，`DeriveTrait` 任务按算子声明的 [`DeriveMode`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/DeriveMode.java)（`LEFT_FIRST`、`RIGHT_FIRST`、`BOTH`、`OMAKASE` 或 `PROHIBITED`）回调父算子。`EnumerableMergeJoin` 声明的是 **`DeriveMode.BOTH`**——左右任一侧交付了覆盖连接键的排序属性，它都能顺势推导出另一侧所需的配对排序并生成新的有序 Merge Join 变体（顺带一提，1.43 开发线修正了其 `implement()` 代码生成中单列布尔连接键被误推断为 `Predicate1` 的问题，显式锁定了 `Function1.class`）。

把自顶向下压需求、缺失属性时的 `Convention.enforce()` 补偿，以及下层天然有序或按分桶分布时的自底向上反哺串在一条时间线上，整套 `PhysicalNode` 协商时序一目了然：

![Calcite Top-Down 模式下 PhysicalNode 双向 Trait 协商与补偿](/images/diagrams/opt-calcite-trait-negotiation.svg)

*图 2：`TopDownRuleDriver` 驱动的双向 Trait 协商——上层通过 `passThroughTraits` 向子 `RelSubset` 下压排序/分布需求并由 `Convention.enforce()` 兜底；下层若天然交付索引顺序或分桶属性，则经 `DeriveTrait` 回调 `deriveTraits` 派生出免除冗余 `Sort`/`Exchange` 的父算子新变体。*

---

## 五、真实运行 Trace 走读：`Join + ORDER BY` 在两种驱动器下的平局与负 CPU 现场

纸上谈兵不如实跑一轮。我们用 `ReflectiveSchema` 构造一个完全可复现的内存测试集：员工表 `emps` 含 `10,000` 行（`deptno = i % 10`），部门表 `depts` 含 `10` 行，运行本文开头的 SQL，并通过 `log4j.logger.org.apache.calcite.plan.volcano.task=DEBUG` 抓取 `CalciteTrace.getPlannerTaskTracer()` 的完整内部轨迹。

送进 `VolcanoPlanner` 前，两条路径面对的初始逻辑计划一模一样：

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

1. **顶层 `LogicalSort` 凭空消失了**：无论是哪条路径，双侧输入经 Enforcer 补上 `EnumerableSort` 喂给 `EnumerableMergeJoin` 后，Merge Join 吐出的有序属性直接满足了根节点的 `ORDER BY e.deptno` 要求，省掉了最外层的二次排序！
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

盯着阶段 ④ 那两行刺眼的 `cpu = -29527.48` 和 `cpu = -4008165.78` 看三秒钟——这就是把源码读透并在真实程序里跑出 Trace 的乐趣：下界估算与上界逐维减法在 `cpu` 维度上早就穿仓成了负数，只因为 `VolcanoCost.isLe` 里的那个 `if (true)` 蒙着眼睛只看 `rowCount`，整台机器才安然无恙地跑到了终点！

---

## 六、框架与一体化内核的分界：对比 StarRocks 及集成避坑指南

把本篇的 Calcite `VolcanoPlanner` 与系列第 4 篇的 [StarRocks Optimizer]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}}) 摆在同一张桌子上对照，你会发现两边每一个架构取舍背后，都是**“可嵌入通用框架”与“垂直一体化 MPP 内核”**的定位差异：

| 核心维度 | Apache Calcite (`VolcanoPlanner`) | StarRocks FE Optimizer |
| --- | --- | --- |
| **物理属性子目标表示** | `RelSubset = RelSet × RelTraitSet`（编码进算子节点） | `Group.lowestCostExpressions`（按 `PhysicalPropertySet` 哈希分桶） |
| **默认代价裁判** | `VolcanoCost` 硬编码只比对 `rowCount` | `realCost = 0.5·cpu + 2.0·mem + 1.5·net` 多维标定 |
| **分支限界剪枝** | 默认驱动器零剪枝；仅 `topDownOpt=true` 且物理候选出现后生效 | `EnforceAndCostTask` 原生内置上界收紧与 `costLowerBounds` 拦截 |
| **属性双向传导位置** | 分散在各物理算子的 `PhysicalNode.passThrough* / derive*` | 集中式 `RequiredPropertyDeriver` + `OutputPropertyDeriver` + `Guarantor` |
| **Enforcer 代价形态** | 默认路径先塞无穷代价 `AbstractConverter`，再靠规则二次展开 | `Exchange` / `Sort` 直接以真实物理算子计价入账 |

StarRocks 非常清楚自家 BE 执行器怎么跑 Hash Shuffle、怎么管内存和 Tablet 主键顺序，所以能把分布属性、网络权重乃至运行期反馈拧成一体；而 Calcite 作为中立底座，不知道宿主底下接的是单机内存迭代器、Flink 流图还是跨引擎联邦查询，因此只能在 Core 层提供可扩展的 Trait 协议、元数据注入点与搜索驱动器，把与执行强耦合的闭环留给宿主引擎自己补齐：

![Calcite 的职责边界：规划、执行与反馈各自由谁负责](/images/calcite-feedback-boundary.svg)

*图 3：Calcite Core 的能力停留在规划侧接口（`RelMetadataProvider`、`Context`、自定义物理 `RelNode` 与 `Convention.enforce()`）；分布式数据交换、Runtime Filter 构建下推以及跨查询历史反馈闭环，均需宿主引擎在执行层自行兑现。*

如果你正准备在自己的数据系统里深度集成或改造 Calcite 的 `VolcanoPlanner`，请务必把下面四条避坑铁律写进团队的设计评审清单里：

1. **想让 CPU、内存或网络字节真正决定计划选型，第一件事就是换掉 `VolcanoCost.FACTORY`**：实现自己的 `RelOptCost` 与 `RelOptCostFactory`，同时检查 `VolcanoPlanner.getCost` 里的 `!zeroCost.isLt(cost)` 零值判定，别让零行高算力算子被悄悄抹平成 `TINY`；
2. **启用多维代价比较前，先修好 `minus()` 减法下溢**：如果在 `topDownOpt = true` 下让 `isLe` 开始检查 CPU 或网络维度，必须保证 `minus()` 扣减上界时的下界估计自洽（或做非负截断保护），否则负 CPU 上界会误杀大批合法物理分支；
3. **打开 `topDownOpt = true` 不是改一行配置就完事**：你的自定义物理算子必须完整实现 `PhysicalNode` 接口（`passThroughTraits`、`deriveTraits` 与恰当的 `DeriveMode`），否则父节点的排序或分布需求根本传不下去；
4. **在自定义 `Convention` 里老老实实写好 `enforce()`**：这是自顶向下模式生成 `Sort` 与 `Exchange` 补偿算子的唯一正规渠道，同时配齐 `canConvertConvention` 与 `useAbstractConvertersForConversion` 的返回契约，才能彻底告别难以调试的 `CannotPlanException`。

## 附：关键源码索引

| 核心模块 | 关键源码路径 |
| --- | --- |
| 搜索总控与累计代价递归 | [`VolcanoPlanner.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoPlanner.java) |
| 单维比较的默认代价实现 | [`VolcanoCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/VolcanoCost.java) |
| 子集账本与状态位掩码 | [`RelSubset.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSubset.java)、[`RelSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/RelSet.java) |
| 默认迭代驱动器与队列 | [`IterativeRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleDriver.java)、[`IterativeRuleQueue.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/IterativeRuleQueue.java) |
| 自顶向下 Cascades 任务栈 | [`TopDownRuleDriver.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleDriver.java)、[`TopDownRuleQueue.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/TopDownRuleQueue.java) |
| 下界元数据计算 | [`RelMdLowerBoundCost.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/metadata/RelMdLowerBoundCost.java) |
| 抽象占位转换器与最小单测 | [`AbstractConverter.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/volcano/AbstractConverter.java)、[`CollationConversionTest.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/test/java/org/apache/calcite/plan/volcano/CollationConversionTest.java) |
| 属性集合与排序定义 | [`RelTraitSet.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/RelTraitSet.java)、[`RelCollationTraitDef.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/RelCollationTraitDef.java) |
| 物理节点属性传导与归并连接 | [`PhysicalNode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/rel/PhysicalNode.java)、[`DeriveMode.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/plan/DeriveMode.java)、[`EnumerableConvention.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableConvention.java)、[`EnumerableMergeJoin.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoin.java)、[`EnumerableMergeJoinRule.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/adapter/enumerable/EnumerableMergeJoinRule.java) |
| Top-Down 开关配置入口 | [`CalciteSystemProperty.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteSystemProperty.java)、[`CalciteConnectionProperty.java`](https://github.com/apache/calcite/blob/e8e0dd54145c44f61b73acad1ffb96c14bddff78/core/src/main/java/org/apache/calcite/config/CalciteConnectionProperty.java) |

---

> **源码基线说明**：文中的源码链接及第 五 节实跑输出的物理计划、代价与 410 条任务 Trace 均基于 `apache/calcite` 提交 `e8e0dd5`（2026-05-04，采用 `Programs.standard()` 阶段编排配合默认 Enumerable 规则集编译运行）。2026-10-28 对照 `1.43.0` 开发线提交 `68f1613`（以及第 六 节对照的 StarRocks `f977b53`）完成了全量复核：`RelSubset` 账本字段、`VolcanoCost` 的 `if (true)` 单维比较、`IterativeRuleDriver`（84 行）、`RelMdLowerBoundCost` 及 Trait 补偿协议均未变化；期间新增的 `TopDownRuleDriver` 栈容器替换（`Stack` → `ArrayDeque`）、`registerImpl` 零列行类型校验（`checkEmptyRowType`）及 `EnumerableMergeJoin` 布尔键 `Function1.class` 显式类型标注已同步补入正文。

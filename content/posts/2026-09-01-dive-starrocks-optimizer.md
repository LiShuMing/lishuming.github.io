---
title: "【源码】深入 StarRocks Optimizer：Enforcer、Property 级联与 Cost 选型"
date: 2026-09-01T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
slug: "dive-starrocks-optimizer"
categories:
  - 数据库
tags:
  - StarRocks
  - Cascades Optimizer
  - 查询优化器
  - CBO
  - Enforcer
  - Physical Property
description: "从分布式 MPP 引擎为什么不能只存“单一最便宜子计划”出发，结合 StarRocks FE 源码剖析 Cascades 协程任务栈、PhysicalPropertySet 级联传导、Exchange/Sort Enforcer 计价闭环，以及 Join 与聚合的真实选型边界。"
draft: false
toc: true
math: true
---

> 本系列第 4/4 篇 · 优化器 / Cascades · [上一篇]({{< relref "2026-09-01-dive-optd-cascades-optimizer.md" >}}) · 相关：[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}})

在[上一篇剖析 optd]({{< relref "2026-09-01-dive-optd-cascades-optimizer.md" >}}) 的结尾，我们撞上了一道横亘在教学原型与分布式生产内核之间的分水岭：单机原型里每个等价类（`Group`）只需要记住唯一一个最便宜的物理子计划（`Winner`）；可一旦放进多机并行的 MPP 引擎，**“谁是最便宜的子计划”这个问题本身就不再有唯一答案**。

不妨在白板上算一笔最直观的账。假设查询要先做两表连接，再按列 `b` 做全局分组聚合：

```sql
SELECT b, COUNT(*)
FROM (
    SELECT t1.a, t2.b
    FROM t1 JOIN t2 ON t1.a = t2.a
) s
GROUP BY b;
```

设想在下层的连接 `Group` 里，有两个合法的物理连接方案：

- **方案甲**：算子自身与扫描总代价只要 **`100`**，但吐出的数据在集群中是按 `Hash(a)` 分布的；
- **方案乙**：利用了某侧分桶布局，算子总代价略贵，需要 **`120`**，但吐出的数据天然就是按 `Hash(b)` 分布的。

如果连接 `Group` 只留一个“最便宜擂主”，方案甲（`100`）就会把方案乙（`120`）淘汰出局。可等到上层的 `GROUP BY b` 算子接手时，麻烦来了：做全局分组聚合要求输入数据必须按 `Hash(b)` 落到同一台机器上！面对按 `a` 分布的方案甲，优化器被迫在连接与聚合之间强插算子——做一次跨网重洗牌（Shuffle `Exchange`），额外花掉 **`50`** 的网络与序列化成本，整棵树总账飙升到 `100 + 50 = 150`；反观刚才差点被扔掉的方案乙，因为输出分布直接命中了上层聚合的胃口，一分钱网络税都不用交，总账定格在 **`120 + 0 = 120`**，反败为胜！

哪怕连接列与聚合列就是同一个等价键（比如 `t1 JOIN t2 ON t1.a = t2.b` 后紧跟 `GROUP BY t1.a`），下层连接究竟选“广播右表（Broadcast）”还是“双侧重分桶（Shuffle）”，也直接决定了上层聚合能不能白嫖连接吐出的数据分布。

看清这笔跨算子的网络账单，分布式 Cascades 优化器的核心设计就呼之欲出了：

1. **多维擂台账本**：每个 `Group` 绝不能只存一个最优解，必须以**物理属性集（`PhysicalPropertySet`：数据分布 + 排序顺序 + CTE 作用域）**为键，分桶保存不同物理契约下的最优子计划；
2. **双向属性传导**：父算子自顶向下提出“我希望孩子满足什么物理属性（`Required Property`）”，子算子自底向上汇报“我实际交付了什么物理属性（`Output Property`）”；
3. **强制补偿计价（`Enforcer`）**：当子计划交付的属性满足不了父算子要求时，优化器自动插入 `Exchange`（重分布）或 `Sort`（重排序）补偿算子，并把这笔路费一分不少地算进候选总成本里同台比价。

StarRocks FE 的查询优化器把这套机制实现得极为清晰紧凑。下面我们就沿着 `QueryOptimizer.optimizeByCost()` 的完整生命周期，拆解任务栈挂起恢复、属性级联、Enforcer 插入，以及生产排障时最常踩坑的 Join 与聚合选型边界。

---

## 一、全局鸟瞰：`optimizeByCost()` 的六阶段流水线与决策边界

很多同学第一次排查 StarRocks 的 `EXPLAIN COSTS` 时容易犯一个归因错误：看到最终计划里少了某个 Shuffle，或者多了一个 `SortAgg` 标记，就跑去 `CostModel.java` 里翻代价公式。实际上，CBO 搜索只是整条规划流水线的中间一环，大量计划变形发生在进 Memo 之前或出 Memo 之后。

入口方法 [`QueryOptimizer.optimizeByCost()`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) 把一次查询规划严格切成了六个阶段：

![StarRocks QueryOptimizer.optimizeByCost() 的六个阶段与 Cascades 任务栈](/images/starrocks-optimizer-lifecycle.svg)

*图 1：`optimizeByCost()` 的六个阶段（左）与阶段 ③ 内部的任务栈挂起/恢复及剪枝闭环（右）。蓝色为前置确定性改写（RBO），橙色为基于 Memo 的代价搜索（CBO），绿色为提取最优树后的物理后处理。*

为什么要摆出“前 RBO → 中 CBO → 后物理改写”的三段式阵型？核心考量是**把 Memo 的搜索预算花在刀刃上**。

在阶段 ① `logicalRuleRewrite` 里，谓词下推（Predicate Pushdown）、列裁剪、子查询解关联、分区裁剪与简单单次 CTE 内联等“做了大概率稳赚不赔”的规范化工作被先行消化，力求把送进阶段 ② `memo.init` 的初始逻辑树压到最精简。同时，优化器把全链路所需的输出列集合放进 `TaskContext.requiredColumns` 随任务一路下传。

即便如此，一旦进入阶段 ③ `memoOptimize` 面对十几张表关联的复杂报表，无节制地展开连接交换律和结合律依然会把内存撑爆。因此 `memoOptimize` 在装载规则集时设了明确的分级熔断阀：

- 当连接表数量达到 `cbo_max_reorder_node` 时，干脆关掉 CBO 内的连接重排，完全仰仗前置启发式顺序；
- 当连接数超过 `cbo_max_reorder_node_use_exhaustive` 时，退化为贪心 `ReorderJoinRule` 加单向交换律，不再做结合律穷举；
- 只有小规模连接才同时放开交换律与结合律探索完整空间，且外键表裁剪规则 `CboTablePruneRule` 也只在连接数小于 `10` 时挂载；
- 连接物理实现规则则按会话变量 `join_implementation_mode` 统一装载（早期的 `isMVPlanner()` 特殊分支已在主干移除）。

等阶段 ④ `extractBestPlan` 沿着属性指针把胜出的物理树从 Memo 里抽出来，阶段 ⑤ `physicalRuleRewrite` 和阶段 ⑥ `dynamicRewrite` 还会做最后一轮“局部外科手术”——比如给满足底层存储有序性的单阶段聚合打上流式 `SortAgg` 标记、把全局单点排序拆成两阶段归并排序、裁剪冗余 Shuffle，最后交由 `PlanFragmentBuilder` 切成跨节点并行执行的 Fragment。

---

## 二、显式任务栈与协程挂起：`EnforceAndCostTask` 如何把上下界拧成闭环

走进最核心的阶段 ③ `memoOptimize`，我们先看清 StarRocks 在内存里搭起的五张核心状态表：

| 核心对象 | 承载的搜索状态与职责 |
| --- | --- |
| `Memo` | 管理全部 `Group`，负责新表达式结构去重（`copyIn`）与补偿算子登记（`insertEnforceExpression`） |
| `Group` | 逻辑等价类；核心账本是 `lowestCostExpressions: Map<PhysicalPropertySet, Pair<Double, GroupExpression>>`（按物理属性分桶记录当前最优解）及 `costLowerBounds`（记录各属性下的已知成本下界） |
| `GroupExpression` | 单个算子加子 `Group` 指针；维护 `(outputProperty → inputProperties)` 映射表（供最终回溯提取子树）、各属性下的计价结果，以及防重入的 `appliedRules` 位图 |
| `PhysicalPropertySet` | 物理契约三元组：`DistributionProperty`（数据分布）+ `SortProperty`（排序方向与空值位置）+ `CTEProperty`（已消费的 CTE ID 集合） |
| `TaskContext` | 单次子目标上下文：携带父节点要求的 `requiredProperty`、`requiredColumns` 以及剪枝天花板 `upperBoundCost`（初始为 `Double.MAX_VALUE`） |

与 `optd` 借用扩栈异步函数递归不同，StarRocks 的 [`TaskScheduler`](https://github.com/StarRocks/starrocks/tree/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/task) 采用了教科书级的显式任务栈 `Stack<OptimizerTask>`：每次从栈顶 `pop` 出一个任务执行前先调 `checkTimeout()` 检查规划超时，彻底杜绝了深层递归爆栈与失控卡死。

栈里流转的五类任务，按后进先出（LIFO）的压栈次序精密配合：

```text
OptimizeGroupTask(G, req, UB)
 ├─ 入口剪枝：若 costLowerBound(req) >= UB 或已存在满足上界的 bestExpr → 直接返回
 ├─ 压入组内逻辑表达式 → OptimizeExpressionTask       （后出栈：晚展开新空间）
 └─ 压入组内物理表达式 → EnforceAndCostTask           （先出栈：早算出可行上界！）
        │
        ├─ RequiredPropertyDeriver 枚举对子节点的候选需求组合
        ├─ 检查当前子 Group 是否已有对应 childReq 的最优解？
        │    ├─ 尚无最优解：
        │    │    1. pushTask(this.clone())            ← 保存当前游标，把自己挂起在栈底
        │    │    2. pushTask(OptimizeGroupTask(child, childReq, UB - curTotalCost))
        │    │    3. return                            ← 让出执行权，先去求解子 Group！
        │    └─ 已有最优解：累加子代价；一旦突破 UB 立即登记下界并放弃当前组合
        ├─ 全部孩子就绪 → ChildOutputPropertyGuarantor 修正 + OutputPropertyDeriver 推导输出
        └─ recordCostsAndEnforce → 满足父需求则直接登记；不满足则追加 Enforcer 算入总账后登记
```

读懂这张图，有三个工程细节最值得拍案叫绝。

第一是 **`rule.promise()` 塑造的探路顺序**。当 `OptimizeExpressionTask` 展开一个逻辑表达式时，会同时收集逻辑变换规则（`promise = 1`）和物理实现规则（`promise = 2`）并按优先级排序，确保物理实现规则先跑；而 `ApplyRuleTask` 一旦产出新的物理表达式，绝不傻等下一轮遍历，当场携带当前 `TaskContext` 压入一个 `EnforceAndCostTask`。两招合用，就是要以最快速度打穿一条到底的物理可行路径，把 `upperBoundCost` 从无穷大拉回真实数字，激活后续全图的分支限界（Branch-and-Bound）剪枝。

第二是 **`EnforceAndCostTask` 手搓的无栈协程（Stackless Coroutine）**。因为 Java 普通方法无法在循环算到第 `i` 个孩子时直接 `yield` 出去等子任务跑完再回来，这个类在成员变量里显式保存了 `curChildIndex`、`prevChildIndex`、`curPropertyPairIndex` 和 `curTotalCost` 四个游标。一旦发现某个子 Group 还没算出 `childReq` 对应的最优解，它就调 `clone()` 把携带当前游标的自己重新压回任务栈，再在头顶压入子 Group 的优化任务（预算收紧为 `UB - curTotalCost`）然后直接返回。等子任务大功告成弹出栈顶，挂起的父任务重见天日，顺着刚才的游标无缝往下累加！

第三是**上下界剪枝闭环**。每当一条物理路径顺利算出总成本且低于当前 `UB`，上下文上界就会立刻收紧；反过来，若累加子成本中途超标，或者恢复执行时发现子任务被上界剪得一个解都没吐出来（`childBestExpr == null`），父任务就会调用 `recordLowerBoundCost` 记下“在这个属性要求下，成本至少超过 `UB`”，下次别的分支带着更紧的预算再撞上来时，在 `OptimizeGroupTask` 第一行就会被直接挡回去。

---

## 三、物理属性级联与 Enforcer 账单：从需求下推到并发安全的补偿插入

搞清了任务怎么跑，我们来看最核心的物理属性（Property）是怎么在算子树里上下穿梭的。StarRocks 没有把这摊逻辑散落在几十个算子类里，而是抽出了职责分明的“三驾马车”：

- **[`RequiredPropertyDeriver`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/RequiredPropertyDeriver.java)（自顶向下提要求）**：绝不盲目枚举所有属性组合，只根据父节点传下来的 `requirementsFromParent` 和当前算子自身的执行契约，生成有限的几组子需求候选（例如 Hash Join 只提“左任意 + 右广播 `[ANY, BROADCAST]`”与“双侧按等值键哈希 `[hash(leftEq), hash(rightEq)]`”两套方案；Merge Join 在此基础上给两侧额外加上等值列 `SortProperty`；全局 `HashAgg` 按分组列提 `SHUFFLE_AGG`，无分组列则提单点汇聚 `GATHER`）；
- **[`OutputPropertyDeriver`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/OutputPropertyDeriver.java)（自底向上报实情）**：根据子计划真实交付的属性和算子物理行为推导输出——比如 Broadcast Join 直接继承左表分布（右表只是复制品），Shuffle Join 按主导侧（Inner 取左、Right Outer 取右、Full Outer 取左并打上空值放宽标记）生成 `SHUFFLE_JOIN` 分布，而一旦开启落盘（Spill）或分区连接，就调用 `resetSortProperty` 诚实抹掉原本的有序属性；
- **`ChildOutputPropertyGuarantor`（横向协同修正）**：等所有孩子都拿到最优解后做最后一轮配对升级——如果发现连接左右两侧都是本地分桶扫描（`LOCAL`）且同属一个 Colocate 组，当场升级为零网络传输的 **Colocate Join**；若左表是 `LOCAL` 而右表需移动，则尝试改造成只搬运右表单侧数据的 **Bucket-Shuffle Join**。

为了让下层算子辛苦建立的数据分布尽可能被上层白嫖，`PropertyDeriverBase` 里还埋了两个极聪明的级联对齐机关：

1. **列序主动对齐**：在多列哈希分布里，`Hash(a, b)` 和 `Hash(b, a)` 算出的分桶号完全不同。如果上层按 `(a, b)` 连接、下层按 `(b, a)` 聚合（或反过来），只要集合相等，`computeShuffleJoinRequiredProperties` 与 `computeAggRequiredShuffleProperties` 就会主动把本层的哈希列顺序拨成和父需求一模一样（外连接场景下还支持 `canRelaxGroupByCols` 空值语义对齐），凭空省掉一层重洗牌。
2. **等价列并查集（`EquivalentDescriptor`）**：`HashDistributionSpec` 内部挂着一个并查集。每经过一层内连接 `t1.a = t2.b`，左右等值列就会被 `unionDistributionCols` 合并（外连接走 `unionNullRelaxCols`）。这样哪怕下层是按 `t1.a` 分布的，到了上层有人点名要 `Hash(t2.b)` 时，一查并查集发现是一家子，直接放行复用！

那么，万一子计划吐出来的 `OutputProperty` 还是没能满足父节点的 `RequiredProperty` 怎么办？这就轮到 **Enforcer（属性补偿算子）** 出手收钱了。

在 StarRocks 里，Enforcer 没有任何魔法特权，就是实打实的普通物理算子：缺数据分布就由 [`DistributionProperty.appendEnforcers`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/DistributionProperty.java) 包一层 `PhysicalDistributionOperator`（落地为 Shuffle / Broadcast / Gather Exchange）；缺有序性就由 [`SortProperty.appendEnforcers`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/SortProperty.java) 包一层 `PhysicalTopNOperator`。整套属性协商、满足性判定与多维账本入库的闭环如下：

![StarRocks EnforceAndCostTask 物理属性协商与 Enforcer 计价闭环](/images/diagrams/opt-starrocks-property-enforcement.svg)

*图 2：`EnforceAndCostTask` 内部的双向物理属性协商——父需求经列序对齐下推、子最优解经并查集与 Colocate 升级回传后，若满足父契约则零补偿直通，否则按序追加 `Distribution` / `Sort` Enforcer 并连同路费一起写入 `lowestCostExpressions`。*

在 `EnforceAndCostTask` 的收官方法 `recordCostsAndEnforce` 里，补偿算子的插入顺序和并发安全都抠到了极致：

- **“只问一次”的并发防竞态设计**：在主干 `f977b53` 中，`recordCostsAndEnforce` 会先把 `satisfyOrderProperty` 和 `satisfyDistributionProperty` 算成两个确定的布尔值传进 `enforceProperty`，而不再像早期版本那样外层判一次 `isSatisfy()`、进函数里又重算一次。为什么要改？因为 `HashDistributionSpec.isSatisfy()` 底层会去查全局的 `ColocateTableIndex`，而后台均衡线程 `ColocateTableBalancer` 随时可能并发把某个正在迁移的 Colocate 组标为不稳定——前后问两次一旦拿到相反答案，就会掉进空分支漏插 Exchange！
- **防死循环的插入次序**：当分布与顺序双双缺失时，如果目标是单点汇聚 `GATHER`，必须**先各节点局部 Sort、再 Gather 归并**；如果是其他分布（如 Shuffle 会打乱顺序），则必须**先做 Distribution 重分区、再在目标端 Sort**。更微妙的是“当前已有序且是 Gather，但新需求既要保持有序又要改成 Shuffle”的边角场景——代码会先重置旧属性再依次补上 `Sort + Distribution`，防止在 `[order, gather]` 与 `[order, shuffle]` 之间死循环横跳。Enforcer 自身一旦包上去，当场通过 `CostModel.calculateCost(enforcer)` 结算路费计入 `Group.lowestCostExpressions`，绝不再递归触发新一轮 `EnforceAndCostTask`。

---

## 四、端到端推演：一条两表连接聚合 SQL 的 15 步栈帧演化

把上面的任务栈、属性并查集和 Enforcer 串起来，我们亲手走读一条经典的连接加聚合查询：

```sql
SELECT t1.a, COUNT(*)
FROM t1 JOIN t2 ON t1.a = t2.b
GROUP BY t1.a;
```

假设基表 `t1` 在存储层本身就按 `a` 列哈希分桶（且单分区桶映射满足本地复用条件），而 `t2` 的分桶列与 `b` 无关。经过前置 RBO 归约后，Memo 里生成了四个干净的 Group：

```text
G0: LogicalAgg(group by a)       ──▶ children: [G1]
G1: LogicalJoin(t1.a = t2.b)     ──▶ children: [G2, G3]
G2: LogicalScan(t1)
G3: LogicalScan(t2)
```

省去无关旁支规则后，任务栈从根节点 `G0` 一路下钻、挂起、触底反弹再提取最优树的 15 个关键瞬间如下：

| 步序 | 弹出栈顶的任务 | 核心状态变迁与协程动作 |
| --- | --- | --- |
| 1–4 | `OptimizeGroupTask(G0, EMPTY, UB=∞)` 及展开任务 | 探索子组 `G1` 的连接交换，调用 `DeriveStatsTask` 算出各组基数统计 |
| 5 | `ApplyRuleTask(AggImplementRule)` | 在 `G0` 生成 `PhysicalHashAggregate`，当场压入 `EnforceAndCostTask(G0-Agg)` |
| 6 | `EnforceAndCostTask(G0-Agg)` | 全局聚合向子组 `G1` 索要 `hash(a) SHUFFLE_AGG`；查账本发现 `G1` 尚无此解 → **`clone()` 挂起自己，压入 `OptimizeGroupTask(G1, hash(a), UB - aggCost)`** |
| 7–8 | `OptimizeGroupTask(G1, hash(a))` → `JoinImplementRule` | 在 `G1` 生成 `PhysicalHashJoin`，压入 `EnforceAndCostTask(G1-Join)` |
| 9 | `EnforceAndCostTask(G1-Join)`（试跑方案 A：广播） | 提出首套子需求 `[ANY, BROADCAST]`；`G2`、`G3` 均无最优解 → **再次挂起自己，下钻叶子 `G2` 与 `G3`** |
| 10 | `EnforceAndCostTask(G2-ScanT1, ANY)` | `t1` 吐出 `LOCAL(a)`，天然满足 `ANY` → 零补偿登记 `G2.best[ANY] = Scan(t1)` |
| 11 | `EnforceAndCostTask(G3-ScanT2, BROADCAST)` | `t2` 本地扫描不满足广播要求 → **追加 `Exchange(BROADCAST)` Enforcer**，以 `scan + broadcast` 总成本登记 `G3.best[BROADCAST]` |
| 12 | `EnforceAndCostTask(G1-Join)` **第 1 次恢复** | 方案 A 双侧就绪！Broadcast Join 继承左孩子 `t1` 的 `LOCAL(a)` 输出，恰好直接满足父任务索要的 `hash(a)`！免插外层 Exchange，登记 `G1.best[hash(a)] = Join(broadcast)` 并收紧成本上界 |
| 13 | `EnforceAndCostTask(G1-Join)`（续跑方案 B：重分布） | 游标切到第二套子需求 `[hash(a), hash(b)]`：左孩子 `G2` 的 `LOCAL(a)` 免网络直通，右孩子 `G3` 插入 `Exchange(SHUFFLE hash(b))`；连接输出 `SHUFFLE_JOIN(a)` 同样满足 `hash(a)`，与方案 A 比拼总成本后保留更便宜者为 `G1.best[hash(a)]` |
| 14 | `EnforceAndCostTask(G0-Agg)` **恢复收官** | 终于拿到 `G1.best[hash(a)]`！累加聚合自身成本，输出满足根要求 `EMPTY`，登记 `G0.best[EMPTY]` |
| 15 | `extractBestPlan(EMPTY, G0)` | 顺着 `GroupExpression` 里记好的 `inputProperties` 链条自顶向下拼出最终物理树 |

看看第 12 步和第 13 步的精妙之处：如果右表 `t2` 只有几千行，广播方案胜出，提取出的是 `HashAgg → HashJoin(broadcast) → [Scan(t1), Exchange(BROADCAST) ← Scan(t2)]`；如果右表 `t2` 有几千万行，广播内存惩罚暴涨，方案 B 逆袭胜出，提取出：

```text
HashAggregate (group by a)                 ← 索要 SHUFFLE_AGG(hash(a))，分文未花直接复用下层输出！
  └─ HashJoin (shuffle, 输出 SHUFFLE_JOIN(a))
       ├─ OlapScan(t1)                     ← LOCAL(a) 原地满足 hash(a)，省掉左表网络重洗牌
       └─ Exchange(SHUFFLE hash(b))        ← Enforcer 唯一插下的一刀
            └─ OlapScan(t2)
```

整棵树里原本可能出现的三次网络洗牌（左表进连接、右表进连接、连接结果进聚合），靠着 `LOCAL(a)` 属性透传与连接输出分布复用，硬生生砍到只剩右表 `t2` 这一处 `Exchange`——而且这一切不是靠哪条写死的启发式特判，完全是 `PhysicalPropertySet` 与 `Enforcer` 统一算总账的自然结果。

---

## 五、Join 选型真相：为什么代码里有 Merge Join 公式，默认却不会选它？

理解了搜索引擎怎么比价，接下来必须擦亮眼睛看清**“到底有哪些选手拿到了入场券”**。如果一个物理算子压根没被实现规则放进 `Group`，它的代价公式写得再漂亮也是空气。

翻开 [`rule/implementation`](https://github.com/StarRocks/starrocks/tree/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/implementation) 目录，确实躺着三条连接实现规则：`HashJoinImplementationRule`（非笛卡尔积且含等值谓词）、`MergeJoinImplementationRule`（含等值谓词）以及兜底非等值连接的 `NestLoopJoinImplementationRule`。但去看 `RuleSet.addAutoJoinImplementationRule()` 就会发现真相：在默认的 `join_implementation_mode = "auto"` 下，**`MergeJoinImplementationRule` 的注册语句至今仍被注释掉（留着 `TODO: implement merge join`）！**

这意味着在默认生产环境里，根本不存在“Hash Join 与 Merge Join 靠 Cost 自动二选一”这回事——等值连接由 Hash Join 独挑大梁，非等值或 Cross Join 由 Nested Loop Join（配合巨额惩罚系数 `EXECUTE_COST_PENALTY × cross_join_cost_penalty`）兜底；只有当你手动把会话变量切成 `SET join_implementation_mode = "merge"` 时，Merge Join 才会入场，通过 `RequiredPropertyDeriver` 向左右两侧索要等值列排序属性，并在缺失有序性时把 `Sort Enforcer` 的账单算进计划里（顺带一提，CBO 后处理里的 [`ExchangeSortToMergeRule`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/ExchangeSortToMergeRule.java) 名字里虽然也有 Merge，干的却是把 `Gather` 之上的全局单点排序拆成“各节点局部排序 + 汇聚归并排序”，和 `PhysicalMergeJoinOperator` 八竿子打不着；而 `CostModel.visitPhysicalTopN` 对未拆分的单阶段全量排序直接判 `infinite()` 无穷大代价，也是为了倒逼这套两阶段归并落地）。

因此，默认模式下 CBO 在连接上的全部火力，其实集中在**连接顺序重排**与 **Hash Join 内部的分发形态博弈（Broadcast vs Shuffle vs Colocate / Bucket-Shuffle）**。在进入具体的哈希连接计价公式之前，不妨先看清 StarRocks 是如何横跨 RBO 与 CBO 治理多表连接搜索空间的：前置阶段先把连续连接拍平进 `MultiJoinNode`，按表规模挑选动态规划（DP）、贪心或左深树定出保底顺序；进入 Memo 后再按表数阈值分级放行 `JoinAssociativityRule` 与 `JoinCommutativityRule`，并靠变换位掩码封死 `A⋈B ↔ B⋈A` 的死循环往复：

![StarRocks 多表 Join 搜索治理：RBO 启发式重排与 CBO 位掩码防环](/images/diagrams/opt-starrocks-join-reorder.svg)

*图 3：多表连接的两段式搜索治理——RBO 阶段利用 `MultiJoinNode` 按表规模给出启发式基线连接树，CBO 阶段在小表群放开结合律与交换律（靠变换位掩码防乒乓环路）、在大表群逐级熔断，最终统一交由 `HashJoin` 物理分发竞价。*

对照 [`HashJoinCostModel.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/HashJoinCostModel.java) 与 [`CostModel.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java)，物理分发这笔账算得非常讲究：

1. **哈希表构建（Build）与内存账单**：
   Broadcast 模式下每台 BE 都要独立建一份完整的右表哈希表，因此构建 CPU 为 `rightOutput`，集群总内存按节点数放大为 `rightOutput × beNum`（且一旦右表行数超过硬红线 `broadcast_row_count_limit` 而左表又不够小，`EnforceAndCostTask.checkBroadcastRowCountLimit` 会在进代价比较前直接一票否决广播方案！）；Shuffle 模式下数据切分到各并行度，构建 CPU 摊薄为 `rightOutput / parallelFactor`（此处 `parallelFactor = max(BE + CN, DOP)`），总内存仅为单份 `rightOutput`。
2. **探测（Probe）的 CPU 缓存未命中惩罚**：
   当右表哈希表膨胀到装不进 CPU 缓存时，左表每次探测都会遭遇随机内存访问延迟。源码用 `mapSize = min(1, keySize) × rightRows` 衡量规模（注意 `keySize` 为等值键平均字节宽之和，套了 `min(1, ...)` 后只要键非空，`mapSize` 实际上就退化为右表行数 `rightRows`），并以 `BOTTOM_NUMBER = 100,000` 行为起征点计算惩罚倍数：
   - **Broadcast 模式**：惩罚因子为 `min(12, max(1, ln(mapSize / 100000)))`，封顶放大 12 倍；
   - **Shuffle 模式**：由于哈希表被并行度切小了，惩罚因子按自然对数减以 2 为底的对数折减为 `min(3, max(1, ln(mapSize / 100000) - log₂(parallelFactor)))`（注意在 `getAvgProbeCost()` 内部，传入的 `parallelFactor` 还额外乘了 `2`），封顶仅放大 3 倍。

最后，算子自身的 CPU 与内存开销，叠加上 Enforcer 产生的跨网传输字节，统一送进 `CostModel.getRealCost()` 按固定权重折算成最终标量总分：

$$\text{realCost} = 0.5 \times \text{CPU} + 2.0 \times \text{Memory} + 1.5 \times \text{Network}$$

看一眼这三个权重系数（内存 `2.0` 最高、网络 `1.5` 次之、CPU `0.5` 最低）就能瞬间理解 StarRocks CBO 的脾气：**它宁可多烧一点本地 CPU，也极度厌恶多机重复驻留大哈希表（重罚 Broadcast 内存）和无谓的全网大表重洗牌（重罚 Shuffle 网络）。**

---

## 六、聚合选型双轨制：Memo 内的多阶段博弈与 Memo 外的 Sort Aggregate

再看分析查询的另一大支柱——分组聚合（Aggregate）。StarRocks 处理聚合选型采用了非常典型的“双轨制”：**拆几个阶段跑，在 Memo 里凭代价硬碰硬；用哈希表还是流式有序聚合，出了 Memo 再看存储脸色。**

先看 Memo 内部的阶段拆分博弈。在规则集初始化时，`SplitTwoPhaseAggRule` 和 `SplitMultiPhaseAggRule` 作为逻辑变换规则挂载进来，把原始的一阶段全局聚合 `GLOBAL agg` 裂变成两阶段 `GLOBAL(split) ← LOCAL(split)`（遇到多个 `COUNT(DISTINCT)` 时还会裂变出三阶段乃至四阶段形态），并把它们全都塞进**同一个 `Group`** 里比价：

```text
Group_Agg:
  ├─ 候选 1：GLOBAL agg                             （一阶段：直接按分组键 Shuffle 后全局聚合）
  ├─ 候选 2：GLOBAL(split) ← LOCAL(split)           （二阶段：先本地预聚合压缩行数，再 Shuffle 全局汇总）
  └─ 候选 3：三/四阶段打散形态                       （针对多列 Distinct 或热点数据倾斜）
```

既然都在同一个 `Group` 里，怎么防止基数估计偏高时选出“本地预聚合一行都没压掉、白建一次哈希表还得全量发网”的蠢计划，或者反过来在含 `DISTINCT` 和高并发下误选单阶段把单节点打爆？StarRocks 用“任务层硬闸门 + 代价层软硬兼施”上了双保险：

- **任务层硬闸门（`canGenerateOneStageAgg`）**：在 `EnforceAndCostTask` 里，只要发现统计信息缺失、包含 `DISTINCT` 或分组列超过 1 个，除非用户强行指定 `new_planner_agg_stage = 1`，或者命中“单 BE 节点 + 开启 `enable_local_shuffle_agg` + **未开启 `enable_query_cache`**（`f977b53` 新增约束）”的本地洗牌豁免，否则一阶段聚合候选当场出局。
- **代价层政策性奖惩（`visitPhysicalHashAggregate`）**：除了对非法一阶段判无穷大（`invalidOneStageAggCost`），一旦发现当前 Group 已经求出了更优的一阶段直通解，再跑出来的冗余二阶段组合会被 `redundantTwoStageAggCost` 直接判死；而在常规场景下，二阶段里的 `LOCAL` 预聚合节点以及它头顶紧挨着的那层 `Exchange` 都会享受 **`× 0.1` 的一折优惠**，鼓励能显著压缩行数的本地预聚合优先胜出。

至于很多人关心的 **Sort Aggregate（流式排序聚合）**，翻遍整个 Memo 你都找不到一个叫 `PhysicalSortAggregate` 的算子——因为它根本不参与 CBO 代价竞争！它的真身藏在阶段 ⑤ `physicalRuleRewrite` 的 [`PhysicalDistributionAggOptRule.UseSortAGGRule`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/PhysicalDistributionAggOptRule.java) 里。只有当 `enable_sort_aggregate = true` 且同时满足以下**五条铁律**时，后置规则才会把胜出的 `PhysicalHashAggregateOperator` 原地打上 `useSortAgg = true` 标记，并通知底下的 `OlapScan` 按 Tablet 内主键有序吐数据（`setNeedSortedByKeyPerTablet(true)`）：

1. 聚合本身是一阶段全局聚合（`isGlobal && groupBy 非空`）；
2. 直接子算子就是基表扫描 `OlapScan`；
3. **只命中了单个物理分区**（一旦跨多个分区，不同分区的相同 Key 散落在不同 Tablet 文件里，没法直接流式归并）；
4. 所有 `GROUP BY` 列都是基表的排序键（Key 列）；
5. `GROUP BY` 列集合恰好覆盖表 Schema 的最左 Key 前缀。

为什么不把 Sort Aggregate 放进 Memo 靠 `SortProperty` 通用推导？道理非常务实：StarRocks 绝不会为了做一个普通聚合去凭空全表排序（哈希聚合快得多），流式聚合唯一的适用甜点就是**免费借用底层存储 Tablet 物理主键前缀的天然有序性**，免掉构建全量分组哈希表的内存开销（同样在这个后置规则里顺手处理的还有按分桶局部性减少冲突的 `usePerBucketOptmize`）。既然只看存储物理事实就能确定性拍板，放在 CBO 之后做轻量标记替换，既省去了 Memo 搜索维度膨胀，又干净利落。

---

## 七、全景排障清单与关键源码索引

走完这趟源码之旅，以后在线上遇到“为什么 StarRocks 没走我期望的 Colocate Join / 一阶段聚合 / 流式聚合”时，千万别一上来就盲猜统计信息不准。把整套优化器的三层筛子摆在手边，按顺序逐层排除，五分钟就能抓到真凶：

```text
第一层：硬准入筛子（进不了代价比较）
  ├─ 规则压根没注册？（如 auto 模式下 Merge Join 未启用；表超阈值关闭详尽 Join Reorder）
  ├─ 规则 check() 或任务硬闸门拦住了？（无等值条件、超 broadcast_row_count_limit、canGenerateOneStageAgg 否决）
  │
第二层：Memo 代价与属性擂台（0.5·CPU + 2.0·MEM + 1.5·NET 数字裁决）
  ├─ 子计划的 OutputProperty 能否靠列序对齐或 EquivalentDescriptor 满足父需求？
  ├─ 补插 Exchange / Sort Enforcer 后的总成本是否反超了另一条看似算子更贵的路径？
  ├─ ChildOutputPropertyGuarantor 是否满足 Colocate 组稳定或 Bucket-Shuffle 转换前提？
  │
第三层：CBO 后置确定性改写（Memo 不可见的局部手术）
  └─ SortAgg 的“单分区 + 最左 Key 前缀 + 一阶段直连 Scan”五条铁律是否全中？两阶段归并排序是否触发？
```

如果你想亲自打开 IDE 断点跟踪某条慢 SQL 的规划全过程，下面这组按调用链排列的源码入口就是最好的路标：

| 核心模块 | 关键源码路径 |
| --- | --- |
| 优化器六阶段总控 | [`QueryOptimizer.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) |
| 显式任务栈与挂起恢复 | [`optimizer/task/`](https://github.com/StarRocks/starrocks/tree/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/task)（重点看 `OptimizeGroupTask`、`EnforceAndCostTask`） |
| 双向物理属性推导 | [`RequiredPropertyDeriver.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/RequiredPropertyDeriver.java)、[`OutputPropertyDeriver.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/OutputPropertyDeriver.java) |
| Enforcer 算子构造 | [`DistributionProperty.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/DistributionProperty.java)、[`SortProperty.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/SortProperty.java) |
| 算子与哈希连接代价模型 | [`CostModel.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java)、[`HashJoinCostModel.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/HashJoinCostModel.java) |
| 连接实现与聚合裂变规则 | [`rule/implementation/`](https://github.com/StarRocks/starrocks/tree/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/implementation)、[`rule/transformation/`](https://github.com/StarRocks/starrocks/tree/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation) |
| 后置 SortAgg 与归并排序改写 | [`PhysicalDistributionAggOptRule.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/PhysicalDistributionAggOptRule.java)、[`ExchangeSortToMergeRule.java`](https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/ExchangeSortToMergeRule.java) |

---

> **源码基线说明**：本文初稿基于 `StarRocks/starrocks` 提交 `0fd27fd`（2026-03-30），并于 2026-10-28 对照上游提交 `f977b53`（2026-09-22）完成了全量源码复核与链接对齐。两快照间优化器目录共有 254 个文件演进，核心 Cascades 任务栈、Property 传导与 `CostModel` 系数（`0.5 / 2.0 / 1.5` 权重、`BOTTOM_NUMBER = 100000`、`12 / 3` 缓存惩罚上限、Auto 模式注释 Merge Join、`UseSortAGGRule` 五条件）均保持不变；主干新增及调整处（移除 `memoOptimize` 中的 `isMVPlanner()` 分支、将 `rule/tvr/*` 演进为 `rule/ivm/*`、`recordCostsAndEnforce` 单次判定属性防 `ColocateTableBalancer` 并发竞态，以及 `canGenerateOneStageAgg` 增加 `!enable_query_cache` 约束）均已同步融入正文。

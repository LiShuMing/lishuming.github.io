---
title: "【Paper】查询优化器演进精读：Volcano/Cascades 搜索框架、函数依赖、属性传递与真实 Join 代价"
date: 2026-10-22T12:10:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "db-papers-cascades-properties-and-cost"
description: "从组合爆炸搜索、物理属性传递非合流反例与简化 I/O 代价公式的 4 倍误判出发，系统精读 Volcano 与 Cascades 搜索框架、Shared-Nothing 零通信并行优化、SQL 三值外连接函数依赖、有限状态自动机顺序推理、SCOPE 并行分区属性格以及即席 Join 真实磁盘缓冲代价模型。"
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "查询优化器", "Cascades", "代价模型"]
series: ["数据库经典论文精读"]
---

> 本系列第 2/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：关系模型与内核架构]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}) · [下一篇：外连接重排与子查询去相关]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}})

关系模型把业务程序员从手写物理导航指针的苦役中解放出来之后，所有的复杂度都浓缩成了一道留给数据库内核的必答题：**面对同一条声明式 SQL，如何在几十毫秒内从数以亿计的等价执行计划中挑出真正跑得最快的那一个？**

如果这只是一道普通的图最短路问题，教科书上的贪心或朴素动态规划早就终结了比赛。然而真实工业级查询优化器之所以成为数据库内核中研发门槛最高的模块，是因为它必须同时在三个相互咬合的维度上对抗组合爆炸与语义陷阱：

1. **搜索空间爆炸与上下文耦合剪枝**：单算 \(n\) 张表的稠密树（Bushy Tree）连接次序，形态数就高达 \(\frac{(2n-2)!}{(n-1)!}\)——当 \(n = 10\) 时超过 **\(1.76 \times 10^{10}\)** 种，再叠加上聚合提前下推、外连接重排与多种物理算子，穷举瞬间就会撑爆内存。更棘手的是，子计划的优劣**高度依赖上层上下文**：一个单看略贵的 `IndexScan` 或 `MergeJoin`，只因产出了按 `(a)` 有序的数据流，就能帮上层 `GROUP BY a` 省掉一次昂贵的全局外部排序。
2. **物理属性与函数依赖推理的微妙暗礁**：为了识别“免排序”与“免重分区”机会，优化器要在百万个候选节点上高频推导排序（Order）、分组（Grouping）与哈希分区（Partitioning）属性。一旦混入 SQL 的 `NULL`、外连接或顺序敏感的多属性函数依赖，看似严密的归约规则会悄悄漏掉最优计划，甚至产出错误结果。
3. **物理硬件与内存缓冲区的非线性真相**：即便搜索框架与属性引擎无懈可击，倘若底层的代价公式（Cost Model）只拿“读写磁盘总页数”拍脑袋估算，忽略多页缓冲（Multi-page I/O Buffer）对磁盘寻道、旋转延迟和哈希工作区的非线性挤压，优化器依然会挑出比真实最优慢 **4 倍以上**的劣质计划。

在进入本篇精读的七篇经典论文之前，我们先在白板上推演两个真实的工业级反例，直观感受物理属性推理与代价估算里隐藏的锋利棱角。

---

## 白板推演：两道让朴素优化器当场失手的反例

### 反例一：顺序属性归约顺序颠倒，白白多排几十 GB 数据

假设底层算子刚刚扫描完主键索引，输出流严格按列 `(a)` 升序排列；而上层 `MergeJoin` 或 `ORDER BY` 要求输入按 `(a, b, c)` 排序。与此同时，优化器从主键与等值谓词中获知两条函数依赖（Functional Dependencies, FD）：

$$a \to b, \qquad \{a, b\} \to c$$

从第一性原理看：既然每一个确定的 `a` 值唯一决定了 `b` 值，而确定的 `(a, b)` 又唯一决定了 `c` 值，那么在任何两行 `a` 相等的元组内部，`b` 和 `c` 也必然完全相等——**按 `(a)` 排好序的数据流，天然就已经按 `(a, b, c)` 排好序了**，根本不需要插入任何 `Sort` 算子！

早期商用优化器（如 Simmen 等人在 IBM DB2 中实现的经典算法）采用“自右向左或按规则逆向消去被决定属性”的重写归约法来判定 `(a)` 是否蕴含 `(a, b, c)`。看看两条不同的归约路径会发生什么：

```text
目标排序要求：(a, b, c)    已知输入有序：(a)    已知 FD：{ a -> b,  (a, b) -> c }

路径 A（先用 (a, b) -> c）：
  (a, b, c)  --[因前缀存在 a, b，消去 c]-->  (a, b)  --[因前缀存在 a，消去 b]-->  (a)  ✅ 匹配成功，免除 Sort！

路径 B（先用 a -> b）：
  (a, b, c)  --[因前缀存在 a，直接消去 b]-->  (a, c)  --[剩余 FD (a, b) -> c 找不到 b！]-->  卡死在 (a, c) ❌ 误判不满足！
```

仅仅因为先应用了 `a -> b` 把中间桥梁 `b` 提前抹掉，重写系统就陷入了死胡同（数学上称为**非合流性，Non-Confluence**），优化器误以为顺序不满足，硬生生往计划树里塞进一个全量 `Sort` 算子！这正是 Neumann 与 Moerkotte（`#13`）用确定有限状态自动机彻底根治的顽疾。

### 反例二：只数读写页数的代价模型，挑出慢 4.14 倍的物理连接

再看代价模型。假设有两张无索引基表 \(R\) 和 \(S\)，每张表大小均为 `10 MB`（按 `8 KB` 页计各占 `1,250` 页，构建内存哈希表膨胀系数 \(F = 1.2\)，即整表装入哈希表需 `1,500` 页 = `12 MB`）。现在系统给这条即席连接分配了 **`13 MB`（`1,625` 页）** 缓冲内存。

如果优化器采用教科书上最常见的“只统计磁盘页传输次数（Transfer-only）”代价公式：无论是 **Hybrid Hash Join** 还是对分块做内存哈希的 **Nested Block Join（NBJ）**，由于 `13 MB > 12 MB` 足以一次性把整张 \(R\) 装入内存，两张表都只需各读一遍，总传输页数同为 `1,250 + 1,250 = 2,500` 页，简化模型立刻判定两者代价完全打平，或者随手挑出默认每分区仅留 1 页缓冲的 Hybrid Hash。然而 Haas 等人（`#06`）在真实磁盘参数（寻道 `9.5 ms`、旋转等待 `8.3 ms`、单页传输 `2.6 ms`）下算完细账后发现：朴素分配下的 Hybrid Hash 耗时高达 **`27.3 s`**，而把剩余 `1 MB`（`125` 页）内存全数用作 \(S\) 表连续大块预读缓冲的 NBJ，把 \(S\) 的随机寻道与旋转次数从 `1,250` 次砍到了 **`10` 次**，总耗时仅需 **`6.6 s`**——粗粒度公式直接造成了 **4.14 倍**的决策误判！

带着这两道直击灵魂的反例，我们沿着**搜索框架 -> 属性与依赖代数 -> 物理 I/O 代价**三层阶梯，依次拆解七篇经典之作。

---

## 搜索框架演进：从 Volcano、Cascades 到零通信分布式并行优化

在 System R（Selinger 1979）开启自底向上动态规划之后，面对日益膨胀的新算子与新语法规则，优化器架构经历了从“手写死循环”到“两阶段规则生成器”，再到“按需探索任务栈”与“集群零通信分区搜索”的三次关键跃迁。下图完整刻画了从 EXODUS MESH 到 Volcano、Cascades 乃至 Shared-Nothing MPQ 的控制流演进：

![查询优化器搜索框架演进：从 Volcano 双阶段 Memo 到 Cascades 按需任务栈与零通信并行拆分](/images/diagrams/paper-opt-volcano-to-cascades.svg)

### Graefe & McKenna (ICDE 1993)：Volcano 优化器生成器——逻辑/物理解耦与有向动态规划 {#paper-03}

- **论文来源**：Goetz Graefe, William J. McKenna, *The Volcano Optimizer Generator: Extensibility and Efficient Search*, IEEE ICDE 1993, pp. 209–218 · [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-volcano-optimizer-generator--extensibility-and-efficient-search.pdf)（核心搜索伪代码 `FindBestPlan` 见 PDF p. 4–5 `Figure 2`；与前代 EXODUS 优化耗时及计划质量对比见 PDF p. 7–8 `Figure 4`）。

在 Volcano 之前，Graefe 参与研发的第一代可扩展优化器生成器 **EXODUS** 曾试图用一张名为 `MESH` 的全局图结构驱动规则重写，却在复杂查询上遭遇了严重的性能滑铁卢。病根在于：**EXODUS 没有区分“逻辑算子”与“物理算法”**——每个 `MESH` 节点把逻辑连接 `Join` 和具体物理算法 `HybridHashJoin` 揉在一起；每当底层子树经规则变换衍生出新形态时，上层所有父节点都必须被迫重新匹配、重新计算代价（Re-analysis），产生海量重复工作：

> "First, Volcano makes a distinction between logical expressions and physical expressions. In EXODUS, only one type of node existed in the hash table called MESH, which contained both a logical operator such as join and a physical algorithm such as hybrid hash join." *(PDF p. 6)*

Volcano 一举确立了延续至今的现代基于规则/代价优化器三大基石抽象（PDF p. 2–4）：
1. **逻辑代数与变换规则（Logical Algebra & Transformation Rules）**：表达查询“算什么”（如 `Join(A, B)` 等价于 `Join(B, A)`），等价表达式共享同一个**逻辑等价类（Equivalence Class）**。
2. **物理代数与实现规则（Physical Algebra & Implementation Rules）**：表达“怎么算”（如将逻辑 `Join` 映射为物理 `HashJoin` 或 `MergeJoin`）。
3. **物理属性强制器（Enforcers）**：本身不改变任何逻辑语义、专门用于修补物理属性（Physical Property Vector，如排序顺序、数据位置）的物理算子，最典型的代表就是 **`Sort`** 与并行洗牌 **`Exchange`**。这种设计让逻辑重写规则完全不必操心物理排序，物理算子也只需声明自己对子节点的输入属性要求。

在搜索引擎层，Volcano 抛弃了 System R 不分青红皂白自底向上填满整张表的做法，提出了自顶向下的**有向动态规划（Directed Dynamic Programming，`Figure 2`，PDF p. 4–6）**。核心入口 `FindBestPlan(LogExpr, PhysProp, Limit)` 接收三个参数：目标逻辑等价类、父算子索要的物理属性向量 `PhysProp`，以及分支定界代价上限 `Limit`。备忘录哈希表按二元组 `(EquivalenceClass, PhysProp)` 索引，记录三种状态：
- 若已算出满足该属性的最优计划，直接 \(O(1)\) 返回；
- 若此前曾以某个代价上限 `PrevLimit >= Limit` 搜索过且确认无解，直接剪枝返回失败（**负结果记忆化**）；
- 若当前等价类正处于递归展开途中（`in-progress` 标志），立即阻断以防双向交换律规则（\(A \bowtie B \leftrightarrow B \bowtie A\)）引发无限死循环。

展开等价类时，算法分三条路径竞标（PDF p. 5–6），其核心控制流可浓缩为如下有向动态规划骨架：

```python
def FindBestPlan(equiv_class, req_prop, cost_limit, excl_prop=None):
    entry = Memo.lookup(equiv_class, req_prop)
    if entry.has_winner():
        return entry.best_plan if entry.best_cost <= cost_limit else None
    if entry.failed_limit >= cost_limit or equiv_class.in_progress:
        return None  # 负结果记忆化剪枝 + 双向交换律防环

    best_plan, best_cost = None, cost_limit
    # 路径 1：枚举物理实现规则（如 HashJoin / MergeJoin）
    for expr in equiv_class.logical_members:
        for phys_op in matching_implementation_rules(expr, req_prop, excl_prop):
            rem_limit = best_cost - phys_op.local_cost()
            child_plans = optimize_children(phys_op, rem_limit)
            if child_plans and total_cost(phys_op, child_plans) < best_cost:
                best_plan, best_cost = build_plan(phys_op, child_plans), total_cost(...)

    # 路径 2：插入物理强制器 Enforcer（如 Sort），并传入 excl_prop 防止底下再垫 MergeJoin
    for enforcer, relaxed_prop in matching_enforcers(req_prop, excl_prop):
        sub_plan = FindBestPlan(equiv_class, relaxed_prop,
                                best_cost - enforcer.cost(), excl_prop=req_prop)
        if sub_plan and sub_plan.cost + enforcer.cost() < best_cost:
            best_plan, best_cost = attach(enforcer, sub_plan), sub_plan.cost + enforcer.cost()

    Memo.record(equiv_class, req_prop, best_plan, best_cost, cost_limit)
    return best_plan
```

注意第 17–19 行的**排除属性向量（`excl_prop`，Excluding Physical Property Vector）**：当上层已经决定插入 `Sort(a)` 时，递归向同一等价类求无序子计划会显式禁止子搜索再挑出天然产出 `(a)` 顺序的算子垫在 `Sort` 底下做无用功。

在 Sun SparcStation-1（约 12 MIPS）上针对 2 至 8 张表连接查询的实测（PDF p. 7–8）显示，Volcano 整个搜索工作区内存不到 **`1 MB`**，复杂查询优化耗时比 EXODUS 快出**约一个数量级**，且在 4 表以上因完整保留了有趣顺序（Interesting Orders）而找到了估算代价更优的物理计划：

> "For more complex queries, the EXODUS' and Volcano's optimization times differ by about an order of magnitude." *(PDF p. 8)*

但 Volcano 留了一条致命的阿喀琉斯之踵：**它的执行被硬生生切成了两个隔离阶段（Two-Phase Separation）**——第一阶段先不看任何物理需求与代价上限，把所有逻辑变换规则跑到底，穷举生成整张 Memo 等价网；第二阶段才启动 `FindBestPlan` 做物理搜索。只要向规则库里加入谓词代数变换、子查询展开或聚合移动规则，第一阶段的逻辑等价表达式数量就会原地爆炸！

### Goetz Graefe (IEEE Data Eng. Bull. 1995)：Cascades 框架——按需探索任务栈与紧致分支定界 {#paper-01}

- **论文来源**：Goetz Graefe, *The Cascades Framework for Query Optimization*, IEEE Data Engineering Bulletin 18(3), 1995, pp. 19–29 · [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-cascades-framework-for-query-optimization.pdf)（六类核心优化任务状态转移图见 PDF p. 2–3 `Figure 1`）。

为了替 Tandem NonStop SQL 与微软 SQL Server 打造下一代工业级优化器，Graefe 在 1995 年推出 **Cascades**，用一套纯 C++ 面向对象抽象基类（`OP-ARG`、`RULE`、`EXPR`）取代了 Volcano 晦涩的代码生成器，并从根基上**废除了 Volcano 的两阶段切分**（PDF p. 1, 3–4）：

> "In the Cascades optimizer, this separation into two phases is abolished, because it is not useful to derive all logically equivalent forms of all expressions, e.g., of a predicate. A group is explored using transformation rules only on demand, and it is explored only to create all members of the group that match a given pattern." *(PDF p. 3)*

Cascades 把递归函数调用彻底拆解为一个显式的**后进先出（LIFO）面向对象任务栈（Task Stack，`Figure 1`）**，由六种职责单一的微任务驱动整个 Memo 图演化：
1. **`Optimize Group`** 与 **`Optimize Expression`**：针对 `(Group, RequiredProp, CostLimit)` 寻找最优物理计划，遍历组内逻辑表达式并按**承诺值（`Promise`）**高低压入规则任务。
2. **`Explore Group`** 与 **`Explore Expression`（按需模式匹配探索）**：这是 Cascades 最精妙的创举！假设某条高层规则只在左子组里存在 `GroupBy` 算子时才能触发，它不会傻傻展开左子组的所有等价形式，而是下发一个带有目标模式（Pattern）的 `Explore Group` 任务，只唤醒有希望拼出该模式的变换规则。每个 Memo 组内部维护一份**模式记忆位图（`Pattern Memory`）**，记录哪些模式已经探索完毕避免重复触发；一旦新生成的表达式经自底向上哈希查重 `(Operator, InputGroups)` 发现两个组其实等价需要合并（Group Merge），它们的模式记忆位图也会同步求交/合并以守住不变量（PDF p. 4, 7–8）。
3. **`Apply Rule`** 与 **`Optimize Inputs`（协程化逐输入动态收紧上界）**：当 `Apply Rule` 产出一个多输入物理算子（例如双输入 `HashJoin`，本地算子开销为 \(C_{\text{local}} = 10\)，父任务给定的总预算上限为 \(L = 100\)）时，`Optimize Inputs` 并不是一口气把两个子组都按宽泛上界推出去，而是像状态机协程一样**每优化完一个子输入就挂起恢复一次，用刚刚拿到的真实子计划最优代价立即收紧下一个子输入的预算上限**：
   - 优化左输入 \(G_1\) 时，传入初始预算 \(L_1 = 100 - 10 = 90\)；假设 \(G_1\) 返回真实最优物理计划代价 \(C_1^* = 65\)；
   - `Optimize Inputs` 再次被调度唤醒，立即算出右输入 \(G_2\) 剩余可用预算仅为 \(L_2 = 100 - 10 - 65 = \mathbf{25}\)！一旦 \(G_2\) 中任何候选分支局部代价超过 `25`，当场剪枝淘汰：

> "Each time after an input has been optimized, the optimize inputs task obtains the best execution cost derived, and derives a new cost limit for optimizing the next input. Thus, pruning is as tight as possible." *(PDF p. 5)*

把这六类任务在 LIFO 栈上的压栈与弹栈次序摊开来，就能看清 Cascades 是怎样用纯循环消除深层递归并按需唤醒规则的：

```text
[栈顶 LIFO 演进轨迹：优化顶层连接组 G0，初始预算 Limit = 100]
1. Push OptimizeGroup(G0, prop=∅, limit=100)
2.   -> Pop 并压入 OptimizeExpr(Join(G1, G2))
3.   -> 按 Promise 高低选出规则：假设“聚合下推穿过连接”要求 G1 内存在 GroupBy 模式
4.   -> Push ExploreGroup(G1, pattern=GroupBy(*))   # 仅按需探索匹配模式，检查 G1.PatternMemory！
5.   -> Push ApplyRule(HashJoinRule, Join(G1, G2))  # 产出物理算子 HashJoin(G1, G2), local_cost = 10
6.   -> Push OptimizeInputs(HashJoin(G1, G2), rem_limit = 90, next_child = 0)
7.      * 第 1 次唤醒：先压入子任务 OptimizeGroup(G1, limit = 90)，自身保留在栈底等待；
8.      * G1 完成后返回最优子代价 C1* = 65；
9.      * 第 2 次唤醒 OptimizeInputs：实时扣减预算 rem_limit = 90 - 65 = 25，
          压入 OptimizeGroup(G2, limit = 25) —— 凡局部开销 > 25 的 G2 候选当场剪枝！
```

此外，Cascades 将谓词本身也提升为一等算子（不再塞在黑盒参数结构体里），允许同一个算子同时开启 `is-logical` 与 `is-physical` 标志，区分探索前的轻量级启发式排序函数 `Promise`（`<= 0` 直接跳过规则）与绑定子树后的精确语义守卫 `Condition`，并引入 `FUNCTION-RULE` 搭配 `LEAF-OP` / `TREE-OP` 通配任意深度的谓词表达式树（PDF p. 6–9）。从微软 SQL Server、Pivotal Greenplum Orca、CockroachDB 到 Apache Calcite、TiDB、StarRocks 与 Doris，今天几乎所有主流分布式与分析型优化器都是这座任务栈大厦的直系后裔。

### Trummer & Koch (2015)：Shared-Nothing 架构上的零通信并行查询优化 {#paper-02}

- **论文来源**：Immanuel Trummer, Christoph Koch, *Parallelizing Query Optimization on Shared-Nothing Architectures*, arXiv:1511.01768v1 (VLDB 2016), EPFL · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/parallelizing-query-optimization-on-shared-nothing-architectures.pdf)（受限幂集笛卡尔积构造伪代码 `AdmJoinResults` 见 PDF p. 6–7；百节点 Spark 集群扩展性实验见 PDF p. 11 `Figure 1–2`）。

单机多核上的并行 Cascades 或并行动态规划通常依赖共享内存里的全局锁或无锁 Memo 表。但在 Spark、Presto 这类本身运行于数十台 **Shared-Nothing（无共享）** 服务器上的大数据系统中，当遇到自动生成的 20 表连接或多目标（Multi-objective，同时权衡执行时间、云主机租用费与内存峰值，需保留整条帕累托前沿 Pareto Frontier）查询时，为什么难以将单机共享 Memo 方案直接照搬到集群上？答案很简单：Memo 表的条目数是表数 \(n\) 的指数函数 \(O(2^n)\)，跨节点同步 Memo 状态的序列化与网络洗牌开销比单机自己重算还要慢上百倍！

Trummer 与 Koch 提出了截然相反的极简哲学 **MPQ（Massively Parallel Query Optimization）**：**把一整条查询的搜索空间静态切分成 \(m = 2^l\) 份互不重叠的顺序约束分区，每个 Worker 节点只领 1 个分区任务，全程零网络通信独立跑完本地动态规划，最后只需汇总各节点的单个最优解（或帕累托集合）**（PDF p. 2–3）：

> "instead of decomposing the query optimization problem into many small optimization tasks, we realize the most coarse-grained problem decomposition possible: the optimization of one query is mapped into exactly one task per worker node." *(PDF p. 2)*

> "This would lead to huge communication overhead on shared-nothing architectures (e.g., the size of the memotable is exponential in the query size) while our algorithm does not require any communication between workers." *(PDF p. 3)*

如果每个 Worker 依然把 \(2^n\) 个表子集全枚举一遍、只在拼装时跳过违规连接，内存和耗时根本降不下来。MPQ 的数学神来之笔在于设计了两组**完全不相交的局部表顺序约束**，让每个 Worker 连违规的中间表子集都无需分配内存（PDF p. 5–9）：
- **左深树（Left-deep Plans，最多 \(m \le 2^{\lfloor n/2 \rfloor}\) 个节点）**：将前 \(2l\) 张表两两配对。Worker 的 `partID` 第 \(i\) 个比特若为 `0`，强制要求在连接序列中 \(Q_{2i} \prec Q_{2i+1}\)（即 \(Q_{2i}\) 必须比 \(Q_{2i+1}\) 更早参与连接）；若为 `1` 则强制 \(Q_{2i+1} \prec Q_{2i}\)。在一对 \(\{Q_1, Q_2\}\) 的 4 个子集中，一旦规定 \(Q_1 \prec Q_2\)，单元素子集 \(\{Q_2\}\) 永远不可能作为合法左深前缀出现，合法子集直接从 `4` 个缩减为 `3` 个（\(\emptyset, \{Q_1\}, \{Q_1, Q_2\}\)）！
  以 4 张表、4 个 Worker（\(l = 2\)）为例（`Example 2`，PDF p. 6），两对比特分别约束 \((Q_1, Q_2)\) 与 \((Q_3, Q_4)\) 的先后次序，整道查询被无重叠地拆成如下 4 个独立工作包：

| Worker `partID` | 解码出的局部先后约束 | 第 1 组受限幂集 \(P_1\) | 第 2 组受限幂集 \(P_2\) | `AdmJoinResults` 笛卡尔并集规模 |
| :--- | :--- | :--- | :--- | :--- |
| `00`（Worker 0） | \(Q_1 \prec Q_2,\; Q_3 \prec Q_4\) | \(\{\emptyset, \{Q_1\}, \{Q_1, Q_2\}\}\) | \(\{\emptyset, \{Q_3\}, \{Q_3, Q_4\}\}\) | \(3 \times 3 = \mathbf{9}\) 个子集（省去 7 个） |
| `01`（Worker 1） | \(Q_1 \prec Q_2,\; Q_4 \prec Q_3\) | \(\{\emptyset, \{Q_1\}, \{Q_1, Q_2\}\}\) | \(\{\emptyset, \{Q_4\}, \{Q_3, Q_4\}\}\) | \(3 \times 3 = \mathbf{9}\) 个子集（省去 7 个） |
| `10`（Worker 2） | \(Q_2 \prec Q_1,\; Q_3 \prec Q_4\) | \(\{\emptyset, \{Q_2\}, \{Q_1, Q_2\}\}\) | \(\{\emptyset, \{Q_3\}, \{Q_3, Q_4\}\}\) | \(3 \times 3 = \mathbf{9}\) 个子集（省去 7 个） |
| `11`（Worker 3） | \(Q_2 \prec Q_1,\; Q_4 \prec Q_3\) | \(\{\emptyset, \{Q_2\}, \{Q_1, Q_2\}\}\) | \(\{\emptyset, \{Q_4\}, \{Q_3, Q_4\}\}\) | \(3 \times 3 = \mathbf{9}\) 个子集（省去 7 个） |

  `AdmJoinResults` 直接对两组大小为 `3` 的合法局部幂集做笛卡尔积并集，一口气构造出全部 **`3 × 3 = 9` 个**合法子集（相比全量 \(2^4 = 16\) 砍掉近一半），连无效状态的判别分支都无需执行！每增加一层二分约束（Worker 数翻倍），单机需保存的中间子集数与 DP 时间复杂度均严格按指数 **\((3/4)^l\)** 衰减。
- **稠密树（Bushy Plans，最多 \(m \le 2^{\lfloor n/3 \rfloor}\) 个节点）**：两表先后关系在分叉树上不再完备（左右两棵独立子树无法定义谁先谁后）。作者遂将表每 3 张分为一组，定义三元树路径偏序 \(Q_x \preceq Q_y \mid Q_z\)：“在连接树中从叶节点 \(Q_z\) 通往根节点的路径上，\(Q_x\) 接入的层级不晚于 \(Q_y\)”。借助一个比特翻转 \(x, y\) 即可把任意稠密树不重不漏地二分（排除同时包含 \(\{Q_z, Q_y\}\) 却不含 \(Q_x\) 的子集，每组 8 个子集剩 7 个），使单节点内存按 **\((7/8)^l\)**、枚举时间按 **\((21/27)^l\)** 指数下降——论文用整数规划严格证明，在固定大小局部约束模板的零通信方案中，\((3/4)\) 与 \((7/8)\) 已经触及理论最优极限（Theorem 4 & 5，PDF p. 8–9）！

在 100 节点 Spark 1.5 集群（最多 256 个并发 Executor）上的实测验证了这套无锁数学拆分的威力（PDF p. 10–12）：128 个 Worker 将 24 表左深树优化提速 **8.1 倍**；64 个 Worker 将 18 表稠密树优化提速 **4.8 倍**；而在单子集需维护数十个帕累托计划的 20 表多目标优化（\(\alpha = 10\%\) 近似剪枝）上，256 个 Worker 实现了 **9.4 倍**加速。

---

## 物理属性与函数依赖推理：从三值外连接 FD 图、有限自动机到并行分区格

Cascades 搭好了按需搜索的骨架，但决定优化器能不能认出“免排序 MergeJoin”和“免洗牌分布式 GroupBy”的大脑，是贯穿整棵计划树的**函数依赖（FD）与结构物理属性（Structural Properties）推理引擎**。下图汇总了 Paulley 外连接严格/松弛 FD 传播、Neumann 确定有限自动机（DFSM）4 字节状态编码，以及微软 SCOPE 全局分区与局部排序/分组非对称蕴含格的完整协同脉络：

![物理属性与函数依赖传递全景——三值外连接 FD 过滤、DFSM 状态机与 SCOPE 并行结构属性格](/images/diagrams/paper-opt-fd-order-partition-traits.svg)

### Glenn Paulley (Waterloo PhD Thesis 2000)：SQL 多重集、三值逻辑与外连接下的函数依赖推理 {#paper-12}

- **论文来源**：Glenn Norman Paulley, *Exploiting Functional Dependence in Query Optimization*, PhD Thesis, University of Waterloo, 2000 · [原始 PDF（共 334 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/%5Bthesis%5D-exploiting-functional-dependence-in-query-optimization.pdf)（松弛依赖传递律失效反例见 PDF p. 60 `Figure 2.1`；过程化推导符号表见 PDF p. 170 `Table 3.3`；嵌套循环排序拼接错误见 PDF p. 254 `Figure 5.2`）。

在纯集合数据库课本里，Armstrong 公理（自反律、增广律、传递律）简洁优美：只要主键 `P.PartID` 函数决定 `P.Description`，查询写 `GROUP BY P.PartID, P.Description` 时就可以安全删掉冗长的文本列 `P.Description`（PDF p. 24）：

> "P.PartID is the key of the Part table there exists the functional dependency P.PartID −→ P.Description. Consequently grouping the intermediate result by both columns is unnecessary—grouping the rows by P.PartID alone is sufficient." *(PDF p. 24)*

然而一旦踏入真实的 ANSI SQL 世界——多重集允许重复行、SQL `UNIQUE` 约束在不少方言里允许存在多行 `NULL`、外连接更是批量制造全 `NULL` 补空行——直接套用 Armstrong 传递律会引发灾难性的错误优化！Paulley 在这部 334 页的博士论文中，首次为带 `NULL` 三值逻辑与外连接的 SQL 多重集建立了严密的**扩展函数依赖图（Extended FD-Graph）**理论（PDF p. 58–62, 87）：

1. **严格依赖（Strict FD, \(X \to Y\)）与松弛依赖（Lax FD, \(X \mapsto Y\)）的生死分野**：
   - **严格依赖 \(X \to Y\)** 要求哪怕两行元组在 \(X\) 的对应列上同为 `NULL`（按 `GROUP BY` 判等语义一致），它们的 \(Y\) 列也必须一致（例如由等值过滤谓词或单表非空主键产生）。
   - **松弛依赖 \(X \mapsto Y\)** 则跳过含 `NULL` 的行，只约束两侧均非空的真实值，**直接丢失了无条件传递律**！看一个只有两行的极简反例（`Figure 2.1`，PDF p. 60）：设表 `R(X, Y, Z) = {(1, NULL, 10), (1, NULL, 20)}`。在 `R` 上，\(X \to Y\) 严格成立（两行 `X=1` 对应的 `Y` 都是 `NULL`）；同时 \(Y \mapsto Z\) 松弛成立（因为根本没有非空的 `Y` 行违反约定）。但拼在一起看，\(X = 1\) 却对应了 `Z = 10` 和 `Z = 20` 两个不同值，\(X \mapsto Z\) 彻底崩塌！论文 `Lemma 2` 严格证明：只有当中间桥梁属性集 \(Y\) 静态保证非空（Definite）时，依赖链才能安全传递。

2. **跨越 `LEFT OUTER JOIN` 补空屏障的可空性判据 \(\eta(p, X)\)（PDF p. 109–110, 191）**：
   当计算 `R LEFT JOIN S ON p` 时，右侧补空表 \(S\) 原本持有的严格主键依赖 \(S.C \to S.D\) 还能不能在连接结果里继续维持严格依赖？乍看之下，所有未匹配的 \(R\) 行都会在右侧补成 `(NULL, NULL)`，似乎会冲毁 \(S\) 的依赖。但仔细推敲：如果所有未匹配补空行填出来的都是整齐划一的 `(S.C = NULL, S.D = NULL)`，唯一能破坏严格依赖 \(S.C \to S.D\) 的凶手，是**某条真实匹配成功的 \(S\) 行自身恰巧带有 `S.C IS NULL` 且 `S.D = 'foo'`**，与补空行的 `(NULL, NULL)` 撞车！
   因此，Paulley 给出精妙的充分条件 `Lemma 7`：只要 `S.C` 本身声明为 `NOT NULL`，或者外连接谓词 \(p\) 是空值拒绝的（例如 `ON R.A = S.C` 在 `S.C IS NULL` 时恒不为 `TRUE`，记作 \(\eta(p, \{S.C\}) = \text{true}\)），真实匹配成功的行里就绝不可能混入 `S.C IS NULL`，从而 \(S.C \to S.D\) 在左外连接输出上**依然是货真价实的严格依赖**！整套图推导算法的时间复杂度被控制在多项式级 \(O(\|V\|^2 + \|E\| \cdot \|P\| \cdot \|V\|)\)（`Claim 37`，PDF p. 191）。

3. **外连接上的空值容忍函数陷阱与嵌套循环伪有序 Bug**：
   - 若投影列包含 `COALESCE(S.x, 0)` 或 `CASE WHEN S.x IS NULL` 等能“看见并改写补空 `NULL`”的标量函数，选择与投影算子便不能随意跨过外连接交换顺序（`Example 13`，PDF p. 77）。
   - 更隐蔽的是 `Figure 5.2`（`Example 34`，PDF p. 253–254）揭露的**嵌套循环排序拼接 Bug**。看下面两张各含两行数据的极简表与连接查询：

```sql
-- Department 表：两个不同部门同驻 'Waterloo'（Location 非唯一！）
-- D = { (DeptID=10, Location='Waterloo'), (DeptID=20, Location='Waterloo') }
-- Employee 表：复合索引 (DeptID, EmpID) 保证每个部门内部按 EmpID 升序
-- E = { (DeptID=10, EmpID=105), (DeptID=20, EmpID=101) }

SELECT D.Location, E.EmpID
FROM Department D JOIN Employee E ON D.DeptID = E.DeptID
ORDER BY D.Location, E.EmpID;
```

   优化器选用索引嵌套循环连接（Index Nested-Loop Join）：外表沿 `Idx_Location` 升序扫描 `Department`，每拿到一行部门，去内表沿复合索引 `(DeptID, EmpID)` 顺序探测员工。许多早期优化器想当然地把外表的 `(D.Location)` 顺序与内表的 `(E.EmpID)` 顺序直接首尾拼接，宣称连接输出天然满足 `(D.Location, E.EmpID)` 有序从而省掉顶层 `Sort`！跑一遍上面的 2 行数据立刻现出原形：第一轮 `DeptID=10` 吐出 `('Waterloo', 105)`，第二轮 `DeptID=20` 吐出 `('Waterloo', 101)`——在同一个 `'Waterloo'` 分组内部，`EmpID` 发生了 `105 -> 101` 的逆序倒挂！论文 `Theorem 19`（PDF p. 260–261）严格证明：**只有当外表排序前缀属性在当前外表流中构成严格键值依赖（即每个 `Location` 至多出现一行）时，内表的局部顺序才能合法追加到全局顺序尾部**。

论文亦诚实标定了理论天花板：推导算法保证了完全的**可靠性（Soundness，Theorem 5，PDF p. 165）**，但依据 Klug 经典不可判定性定理，任意关系代数表达式上的函数依赖完备闭包在理论上不可判定（PDF p. 135）：

> "Perhaps most importantly, we cannot guarantee that the algorithms below determine all possible dependencies in F+. Klug [162] showed that given an arbitrary relational algebra expression e it was impossible to determine all the dependencies that held in e." *(PDF p. 135)*

这套扎实的依赖引擎直接落地于商用数据库 Sybase SQL Anywhere，支撑起生产级的 `GROUP BY` 列裁剪、`DISTINCT` 消除与冗余连接裁剪（PDF p. 274）。

### Neumann & Moerkotte (2003)：基于有限状态自动机的高效顺序属性优化框架 {#paper-13}

- **论文来源**：Thomas Neumann, Guido Moerkotte, *An Efficient Framework for Order Optimization*, Reihe Informatik 11/2003, University of Mannheim (VLDB 2004) · [原始 PDF（共 24 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/an-efficient-framework-for-order-optimization.pdf)（顺序 NFSM 与确定化 DFSM 构造见 PDF p. 9 `Figure 1–2`；四步编译预处理流水线见 PDF p. 10 `Figure 3`）。

Paulley 解决了“哪些函数依赖合法存活”，接下来的工程瓶颈落到了**单次属性检查的极致速度与内存占用**上。在动态规划或 Cascades 枚举期间，优化器要对成百上千个中间计划执行数以百万次的顺序蕴含检查 `contains(deliveredOrder, requiredOrder)` 与新算子顺序推导 `inferNewLogicalOrderings`（PDF p. 3）：

> "Since these two operations are called millions of times during plan generation, they are highly performance-critical. The third crucial parameter is the space requirement for annotating every plan node with its output order." *(PDF p. 3)*

并且如本文开头反例一所示，Simmen 等人的传统逆向归约算法不仅每次都要跑字符串/列表重写，还会因非合流性在 `{a -> b, (a, b) -> c}` 上把完全合法的 `(a)` 误判为不满足 `(a, b, c)`（PDF p. 8）：

> "Note that if we applied a → b first, then (a, b, c) would reduce to (a, c) and no further reduction is possible. Hence, the rewrite system induced by their reduction process is not confluent. This problem is not mentioned by Simmen et al., but can have the effect that contains returns false whereas it should return true." *(PDF p. 8)*

Neumann 与 Moerkotte 敏锐地洞察到：尽管单条流经 FD 扩展出的逻辑等价排序可能有无穷多条，但**在特定的一条查询内部，所有物理算子能产出的顺序集合 \(O_P\) 与所有上层算子会索要测试的顺序集合 \(O_T\)（合称有趣顺序 \(O_I = O_P \cup O_T\)）是完全有限且在编译期静态已知的**！

于是他们把运行期昂贵的代数证明，巧妙转化为一次性的查询级**有限状态自动机（FSM）预编译**（PDF p. 6–18）：
1. **严格区分三类依赖的自动机转移边**：
   - 一般函数依赖 \(a \to b\)：只能在已读入前缀中出现过 \(a\) **之后**追加 \(b\)（把状态 `(a)` 推进到 `(a, b)`），绝不允许抹去 \(a\) 变成 `(b)`（例如按员工工号排序的数据，虽然工号决定部门号，但数据绝没有按部门号全局排好序！）；
   - 等值连接谓词 \(a = b\)（双向严格互决）：允许在排序列中自由将 \(a\) 替换为 \(b\)，因而 `(a)` 直接等价于 `(b)`；
   - 常量过滤谓词 \(\emptyset \to a\)（全表列值恒定）：允许在任意排序位置无条件插入 \(a\)。
2. **四步剪枝构造与子集确定化（`Figure 3`，PDF p. 10–17）**：先围绕当前查询的有趣顺序集合 \(O_I\) 与可能生效的 FD 集构造非确定有限自动机（NFSM）；紧接着执行剪枝——用算子过滤器 \(\Omega_N\) 剔除与查询无关的 FD、合并人工辅助节点 \(Q_A\)、截断超过最长有趣顺序前缀的冗余后缀；最后通过标准子集构造法（Subset Construction）转化为**确定有限状态自动机（DFSM）**，并离线预算出两张微型查找表：**状态兼容性位向量矩阵（Compatibility Matrix）**与**FD 集状态转移表（Transition Table）**。

这套编译带来的运行时收益令人叹为观止：**计划树上的每一个物理算子节点，只需要存储一个 4 字节整数（`int stateID`）就能同时代表它隐含的成百上千种等价逻辑排序**！原本耗时复杂的 `contains` 检查与算子引入新等值谓词后的 `infer` 升级，在查询生成热路径上直接坍缩为两行零分支查表代码：

```cpp
// PlanNode 仅驻留 4 字节状态号：int orderState;
// 1. 判定当前算子输出顺序 delivered 是否满足父算子要求 required（O(1) 位测试）
inline bool contains(int deliveredState, int requiredOrderIdx) {
    return (compatBitMatrix[deliveredState] & (1ULL << requiredOrderIdx)) != 0;
}

// 2. 经过等值连接或选择谓词（引入新 FD 类 fdClassId）后推导新顺序状态（O(1) 数组寻址）
inline int inferNewOrderings(int inputState, int fdClassId) {
    return fdTransitionTable[inputState][fdClassId];
}
```

在 Athlon XP 1800+ 上的实测数据极具说服力（PDF p. 19–22）：对于包含多表连接的 TPC-R Q8，剪枝将 NFSM 节点从 `376` 压至 `38`、DFSM 状态从 `80` 压至 **`24` 个**，整套查找表仅占 **`912` 字节**，自动机预编译耗时仅 **`0.2 ms`**；由于剪枝自动合并了对上层查询毫无区分度的细碎顺序差异，动态规划得以更早淘汰次优子计划，候选子计划数从 `200,536` 降至 `123,954`，整体计划生成时间从 `262 ms` 缩短至 **`52 ms`（提速 5.04 倍）**；而在 10 表 \(n+1\) 边复杂图查询上，优化总耗时更是从 `202,832 ms` 暴跌至 **`3,021 ms`（提速 67.14 倍）**！

### Zhou, Larson & Chaiken (Microsoft SCOPE)：将分区、排序与分组统一纳入 Cascades 并行搜索 {#paper-14}

- **论文来源**：Jingren Zhou, Per-Åke Larson, Ronnie Chaiken, *Incorporating Partitioning and Parallel Plans into the SCOPE Optimizer*, IEEE ICDE 2010, Microsoft Corp. · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/incorporating-partitioning-and-parallel-plans-into-the-scope-optimizer.pdf)（并行 `Exchange` 拓扑与归并算子见 PDF p. 3 `Fig. 2`；Cosmos 集群生产日志查询优化前后计划对比见 PDF p. 11 `Fig. 3`）。

当数据库从单机走向成百上千台机器组成的分布式集群（如微软支撑必应搜索后台的 Cosmos / SCOPE 大数据平台），单机优化器津津乐道的局部 `Sort` 代价，在跨机房网络洗牌（Repartition / `Exchange`）面前瞬间成了小巫见大巫。早期分布式系统常用“两阶段法”偷懒：先用单机优化器挑一棵最优串行计划，再机械地往算子之间塞并行 `Exchange`。这种割裂设计完全看不见“多花一点局部排序代价、换取彻底省掉下游跨网重分区”的全局最优解。

微软团队选择在 Cascades 框架内用一套统一的**结构物理属性（Structural Properties）**同时建模串行、并行与混合计划（PDF p. 1–5）：

$$\mathcal{S} = \big\{\; P^\theta \;;\; \{A_1, A_2, \dots, A_n\} \;\big\}$$

其中前半部分 \(P^\theta\) 刻画**全局跨节点数据分区方式**（包括未分区的单流 \(\bot\)、无规则随机分区 \(\emptyset\)、全节点广播副本 \(\top\)，以及按列集求哈希 `Hash(X)` 或按区间切分 `Range(X)`）；后半部分 \(\{A_1, \dots, A_n\}\) 则按严格先后次序刻画**每个分区内部数据流满足的局部排序动作 \(C^o\) 与局部分组动作 \(C^g\)**（例如 \(\{ \{a\}^g, \{b\}^o \}\) 表示分区内相同 `a` 的行已经聚成连续块，且在每个 `a` 块内部按 `b` 有序）。

研读本文最核心的第一性原理收获，是理解**局部有序性与全局哈希分区在蕴含方向上的镜像反转定律（PDF p. 5）**：
- **局部排序与分组是“由细向粗截断前缀”（前强后弱）**：如果单个分区内的数据按 `(a, b)` 排好了序，它天然就已经按前缀 `(a)` 排好了序，即 \((a, b)^o \Rightarrow (a)^o\)；同理，先按 `a` 分组再在组内按 `b` 分组，天然保证相同 `(a, b)` 的行聚在一起，但反过来仅把相同 `(a, b)` 聚成块（如 `(1,10), (2,20), (1,30)`），相同的 `a=1` 仍可能散落在前后两处，因此：

> "Note that {C1^g, C2^g} ⇒ {{C1, C2}^g} but {{C1, C2}^g} ⇏ {C1^g, C2^g}." *(PDF p. 5)*

- **全局无序分区（如 Hash 分区）恰恰相反，是“由少向多超集扩张”（少强多弱）**！假设数据按单列 `Hash(a)` 分发到 100 台机器：所有 `a = 1` 的行必定落在同一台机器上，那么所有 `(a = 1, b = 10)` 的行当然也必定都在这一台机器上——因此 **`Hash(a)` 自动满足对超集 `(a, b)` 的同机共址（Co-location）要求**！反过来，倘若数据按双列组合 `Hash(a, b)` 分发，`(1, 10)` 可能被哈希到机器 #3，而 `(1, 20)` 被哈希到机器 #87，此时若上层想按单列 `GROUP BY a` 聚合，`Hash(a, b)` **完全不满足**对 `(a)` 的分区要求，必须走网络重洗牌！

一旦把这套结构属性接上函数依赖与等值类推理（PDF p. 2, 6–9），惊人的免洗牌优化便水到渠成。看原文第 2 页给出的经典分布式连接后聚合查询：

```sql
-- 已知 R.c 是表 R 的主键，因此存在严格函数依赖 R.c -> R.a
SELECT R.c, S.d, COUNT(*)
FROM R JOIN S ON R.a = S.b
GROUP BY R.c, S.d;
```

分布式连接算子刚按连接键 `Hash(R.a)`（因 `R.a = S.b`，等价于 `Hash(S.b)`）在百台机器上做完重分区连接，表面上看当前流的分区键是 `{R.a}`，跟上层 `GROUP BY` 索要的 `{R.c, S.d}` 八竿子打不着。但结合两条属性公理一推演：（1）既然主键 `R.c -> R.a`，任何拥有相同 `R.c` 值的元组必然拥有完全相同的 `R.a` 值，因此按 `Hash(R.a)` 分发后，相同 `R.c` 的所有行**必定已经落在了同一台物理节点上**（即数据流隐式满足共址属性 `Part(R.c)`）；（2）再由全局分区的超集扩张律，满足 `Part(R.c)` 的数据流自动满足超集 `Part(R.c, S.d)`——整场跨机房二次网络重洗牌被直接凭空消去，每台机器原地即可完成最终聚合：

> "If a functional dependency R.c → R.a exists (for instance, R.c is the primary key of R), the join result is also partitioned on R.c so that we can safely perform aggregation without repartitioning the data." *(PDF p. 2)*

为了避免父算子枚举成百上千种可接受的子分区组合，SCOPE 将上层属性需求紧凑编码为格空间上的闭区间 \([P_{\min}, P_{\max}]\)（外加非空标志），把复杂的并行 `Exchange` 择优收敛在极小开销内。在 84 台服务器组成的生产级 Cosmos 集群上处理 `173 GB` 与 `264 GB` 两份海量事件日志（默认 150 个分区，见 `Fig. 3`，PDF p. 10–11）时，SCOPE 优化器自动将下游连接的 `guid` 列提拔为上游两个 `DISTINCT` 子查询的哈希分区前缀与局部排序首列，一举省掉两次全量重分区与重排序，在**编译耗时不足 `1 秒`**的前提下，把端到端作业运行时间从 **`21 分钟`直接砍半至约 `11 分钟`**！

---

## 物理代价真相：内存缓冲区博弈如何颠覆教科书 Join 公式

搜索框架不漏候选，属性自动机不插冤枉 `Sort` 与 `Exchange`，最后把关定夺的裁判只剩**物理代价模型（Cost Model）**。如果裁判手里的计分牌本身失真，前面所有的精妙架构都会沦为南辕北辙的帮凶。

### Haas, Carey & Livny (1993)：追寻即席连接代价的真相 {#paper-06}

- **论文来源**：Laura M. Haas, Michael J. Carey, Miron Livny, *Seeking the Truth About Ad Hoc Join Costs*, Technical Report #1148, Univ. of Wisconsin–Madison & IBM Almaden, May 1993 (VLDB Journal 1997) · [原始 PDF（共 27 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/cost-model/seeking-the-truth-about-ad-hoc-join-costs.pdf)（三元组详细磁盘代价公式与 Fujitsu M2266 参数表见 PDF p. 5–6 `Table 1`；缓冲分配偏差与三类代价模型误判对比见 PDF p. 22–23 `Table 3–4`）。

长期以来，大量学术论文在评测新连接算法时习惯用极度简化的度量指标——要么只数读写了多少个磁盘页（`Transfer-only`，假定每页 I/O 代价恒定），要么只数发起了多少次系统 I/O 调用（`I/O-count-only`），并且拍脑袋假设每次读写只用 1 页缓冲或对半平分内存。三位作者决定彻底算清无预建索引的**即席等值连接（Ad Hoc Equijoin）**账本，把五种经典物理连接——**分块嵌套循环（NBJ，对内存中的 \(R\) 分块建立哈希表）**、**排序归并（SMJ）**、**Simple Hash**、**Grace Hash** 与 **Hybrid Hash**——放在统一的真实机械磁盘三维代价方程下同台竞技（PDF p. 5–6）：

$$C_T = N_S \cdot T_S \;+\; N_{I/O} \cdot T_L \;+\; N_X \cdot T_X$$

以文中基准磁盘 Fujitsu M2266（页大小 \(P = 8\text{ KB}\)）为例：单次磁头寻道耗时 \(T_S = 9.5\text{ ms}\)，半圈旋转延迟 \(T_L = 8.3\text{ ms}\)，而真正把 `8 KB` 字节传进内存的传输时间仅需 **\(T_X = 2.6\text{ ms}\)**！单页随机读一次要花 `9.5 + 8.3 + 2.6 = 20.4 ms`，而一次连续顺序读 `64` 页平均每页只需 `2.88 ms`，相差超过 **7 倍**！

一旦引入多页块缓冲（Multi-page I/O Buffering），所有连接算子内部都浮现出同一场尖锐的**内存零和博弈**：
- 把更多内存页划给输入/输出连续读写缓冲，能成倍削减寻道次数 \(N_S\) 与旋转等待次数 \(N_{I/O}\)；
- 但这会直接挤占留给算子核心工作区（如 NBJ 装 \(R\) 分块的哈希表 \(M_R\)、SMJ 生成初始顺串的败者树工作区 `WS`、Grace/Hybrid Hash 驻留内存的 `R_0` 分区）的页数，一旦工作区缩水跨越离散台阶，就会触发额外一轮全表扫描或临时文件落盘，导致总传输页数 \(N_X\) 阶梯式暴涨！

让我们亲手算一遍原文第 17–18 页关于 **NBJ 离散台阶调优**的经典算例，体会连续微积分导数与离散整数天花板碰撞出的工程智慧：
- 设总可用缓冲内存 \(M = 500\) 页（`4 MB`），基表 \(|R| = |S| = 1,250\) 页（各 `10 MB`），哈希表结构膨胀系数 \(F = 1.2\)，故完整装下 \(R\) 共需 \(F|R| = 1,500\) 页。
- 若直接对连续化的 I/O 时间公式求导找极值点，算出给内表 \(S\) 留读缓冲 \(M_S = 37\) 页、给外表 \(R\) 留哈希工作区 \(M_R = 463\) 页。按此切分，\(R\) 必须拆成 \(NB = \lceil 1,500 / 463 \rceil = \mathbf{4}\) 个分块轮流装入内存，内表 \(S\) 随之要被重扫 4 遍；每扫一遍 \(S\)，需要发起 \(\lceil 1,250 / 37 \rceil = \mathbf{34}\) 次磁盘块读取。
- 但请停下来想一想：**既然横竖都要扫 4 遍 \(S\)，拿 `463` 页装每块 \(R\) 纯属浪费**！把 1,500 页均匀切成 4 块，每块其实只需要 \(M_R' = \lceil 1,500 / 4 \rceil = \mathbf{375}\) 页！把省下来的 `463 - 375 = 88` 页全数转拨给 \(S\) 的连续读缓冲，使 \(M_S'\) 从 `37` 页暴涨至 **`125` 页**——此时 \(R\) 的分块轮数依然是 4 轮（总页传输数 \(N_X\) 分毫不增），但每轮扫描 \(S\) 的磁盘 I/O 次数瞬间从 `34` 次骤降到 \(1,250 / 125 = \mathbf{10}\) 次，白捡了近百次磁头寻道与旋转等待！
- 同理，当内存充裕到 \(M = 1,625\) 页（`13 MB`）时，早期文献（Hagmann 1986）曾给出“把 50% 内存分给内表 \(S\) 缓冲最优”的经验法则；本文严格证明该法则仅在 \(T_L/T_X \to \infty\) 且 \(|S|/M \to \infty\) 的双无穷极限下才成立——在真实磁盘上若真分一半内存给 \(S\)，原本 1 趟就能全装进内存的 \(R\) 会被迫拆成 2 趟！真正的最优解是给 \(R\) 留足 `1,500` 页保住单趟扫描，把剩余约 `4%`（或 `125` 页）全给 \(S\) 做大块预读（PDF p. 14, 18）：

> "For the larger memory, the minimum occurs at around 4%, or 63 pages (this is the largest amount of memory we can give to S and still have R fit in memory in one chunk, minimizing transfer costs)." *(PDF p. 14)*

> "by optimizing the buffer allocations, we reduced the negative effect of the excessive I/Os that were occurring with the naive allocations. Thus, the effect of page transfers was increased — but only because we significantly reduced the number of I/Os." *(PDF p. 24)*

论文 `Table 4`（PDF p. 23）用一组震撼的对比数据为全文画上句号（\(|R|=10\text{ MB}, |S|=10\text{ MB}\)，衡量不同简化代价模型选出的计划在真实三维磁盘上的实际耗时）：

| 可用缓冲内存 \(M\) | 只数传输页数模型（`Transfer-only`） | 只数 I/O 次数模型（`I/O-count-only`） | 完整三维磁盘加缓冲调优模型（`Detailed`） | 简化模型最大慢速比 |
| :--- | :--- | :--- | :--- | :--- |
| **`1 MB`（紧缺两趟区）** | 选朴素 Hybrid Hash -> 实耗 **`83.4 s`** | 选大块 NBJ -> 实耗 **`85.7 s`** | 选缓冲调优 Grace/Hybrid -> 实耗 **`26.6 s`** | **3.22x**（`85.7 / 26.6`） |
| **`13 MB`（单趟临界区）** | Hybrid Hash 与 NBJ 判平 -> 实耗 **`27.3 s`** | 选大块 NBJ -> 实耗 **`6.6 s`** | 锁定 `125` 页预读缓冲 NBJ -> 实耗 **`6.6 s`** | **4.14x**（`27.3 / 6.6`） |

看清这张表的深意：在 `1 MB` 紧缺内存下，`I/O-count-only` 贪图单次大块读写调用少，盲目选了需要反复重扫内表的 NBJ，输了 3.22 倍；到了 `13 MB` 充裕内存下，`Transfer-only` 只看到两张表各读一遍，对 Hybrid Hash 默认单页缓冲引发的上千次随机磁头抖动完全失明，又输了 4.14 倍！

即便放到今天 NVMe SSD 与云对象存储普及的时代，这条第一性原理依然锋利如初：随机 `4 KB` IOPS 延迟与顺序 `1 MB` 吞吐带宽之间依然横亘着数倍至数十倍的鸿沟，而内存哈希工作区一旦被读写缓冲挤破阈值触发落盘（Spill to Disk），延迟照样会出现断崖式跳变——**脱离执行器真实内存切分策略与存储硬件延迟谱系的代价公式，永远只是空中楼阁**。

---

## 横向对比：现代工业级优化器的三层协同全景

将本篇精读的七篇文献串联起来，恰好拼成了一台现代工业级查询优化器（如 SQL Server、Greenplum Orca、TiDB、StarRocks、Apache Doris）从顶层任务调度、中层属性推导到底层物理计价的完整精密机芯：

| 论文与年份 | 所属优化器层级 | 解决的核心第一性原理矛盾 | 关键数据结构 / 算法原语 | 代表性量化收益与工程边界 |
| :--- | :--- | :--- | :--- | :--- |
| **[#03](#paper-03) Volcano (1993)** | 搜索框架层 | 消除 EXODUS 逻辑与物理混杂引发的重复重算 | 逻辑等价类、物理强制器 `Enforcer`、有向 DP + 负结果记忆化 | 工作区 `< 1 MB`，复杂查询编译比 EXODUS 快约 **10 倍**；受限于第一阶段全量逻辑展开 |
| **[#01](#paper-01) Cascades (1995)** | 搜索框架层 | 废除先穷举逻辑再物理择优的两阶段爆炸墙 | 六类微任务 LIFO 栈、按需 `Pattern Memory`、`Optimize Inputs` 动态收紧预算 | 实现最紧致分支定界剪枝；确立现代商业与开源优化器事实标准骨架 |
| **[#02](#paper-02) MPQ 并行优化 (2015)** | 分布式搜索层 | 破解 Shared-Nothing 集群跨网同步指数级 Memo 瓶颈 | 零通信粗粒度拆分、局部顺序约束幂集笛卡尔积 `AdmJoinResults` | 内存与耗时按 \((3/4)^l\)（左深）及 \((7/8)^l, (21/27)^l\)（稠密）衰减；Spark 百节点最高加速 **9.4 倍** |
| **[#12](#paper-12) Paulley FD 理论 (2000)** | 依赖与语义层 | 修复 `NULL` 三值逻辑与外连接补空击穿 Armstrong 传递律 | 扩展 FD 图、严格依赖 \(\to\) vs 松弛依赖 \(\mapsto\)、外连接可空判据 \(\eta(p, X)\) | 多项式时间可靠推导；堵住嵌套循环非唯一前缀排序拼接等隐蔽正确性漏洞 |
| **[#13](#paper-13) Neumann 顺序框架 (2003)** | 物理属性推理层 | 根治逆向重写非合流漏判与百万次属性检查开销 | 有趣顺序剪枝 NFSM -> 确定化 DFSM + **每节点 4 字节整数状态** | `contains` 与 `infer` 降至 \(O(1)\) 查表；TPC-R Q8 快 **5 倍**（表仅 `912 B`），10 表查询快 **67 倍** |
| **[#14](#paper-14) Microsoft SCOPE (2010)** | 并行结构属性层 | 打通分布式分区与单机排序/分组的联合代价搜索 | 统一结构属性 \(\{P^\theta; \{A_i\}\}\)、局部截断 vs 全局超集非对称律、区间需求 \([P_{\min}, P_{\max}]\) | 借 FD 消除冗余网络洗牌；84 节点百 GB 日志作业从 `21 min` 降至 **`11 min`**（编译 `< 1 s`） |
| **[#06](#paper-06) Haas 即席 Join 代价 (1993)** | 物理代价模型层 | 纠偏单维页计数公式对多页缓冲与寻道开销的失明 | 三维磁盘公式 \(N_S T_S + N_{I/O} T_L + N_X T_X\) + 离散整数块缓冲调优 | 揭示连续导数外还需离散天花板对齐；避免简化模型高达 **3.1–4.14 倍**的算法错选 |

当一条复杂的分布式分析 SQL 进入现代引擎时：Cascades 任务栈（`#01`）按需唤醒变换与实现规则（超大规模多目标场景可借 `#02` 无通信切分扩展）；每当算子向输入索要排序或分区属性时，底层由结合了严格/松弛函数依赖图（`#12`）、\(O(1)\) 状态机（`#13`）与全局—局部结构属性格（`#14`）的 Trait 引擎判定能否免除 `Sort` 和 `Exchange`；最终每个候选物理算子根据真实的可用内存配额与硬件延迟参数（`#06`）报出精确代价，反馈回 `Optimize Inputs` 剪除昂贵分支。

然而，在真实业务 SQL 里，横亘在优化器面前的还不只是内连接与分组——一旦查询里出现破坏结合律的 `LEFT/FULL OUTER JOIN` 和破坏空集单调性的关联子查询（`NOT EXISTS`、`COUNT(*) = 0`），连把 SQL 安全放进 Memo 等价组的前提都会动摇。这正是本系列下一篇《外连接重排与子查询去相关演进》即将正面攻坚的战场。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#03**：Goetz Graefe, William J. McKenna. *The Volcano Optimizer Generator: Extensibility and Efficient Search*. IEEE ICDE 1993, pp. 209–218. [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-volcano-optimizer-generator--extensibility-and-efficient-search.pdf)
2. **#01**：Goetz Graefe. *The Cascades Framework for Query Optimization*. IEEE Data Engineering Bulletin 18(3), 1995, pp. 19–29. [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-cascades-framework-for-query-optimization.pdf)
3. **#02**：Immanuel Trummer, Christoph Koch. *Parallelizing Query Optimization on Shared-Nothing Architectures*. arXiv:1511.01768v1 (VLDB 2016). [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/parallelizing-query-optimization-on-shared-nothing-architectures.pdf)
4. **#12**：Glenn Norman Paulley. *Exploiting Functional Dependence in Query Optimization*. PhD Thesis, University of Waterloo, 2000. [原始 PDF（共 334 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/%5Bthesis%5D-exploiting-functional-dependence-in-query-optimization.pdf)
5. **#13**：Thomas Neumann, Guido Moerkotte. *An Efficient Framework for Order Optimization*. Technical Report 11/2003, Univ. of Mannheim (VLDB 2004). [原始 PDF（共 24 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/an-efficient-framework-for-order-optimization.pdf)
6. **#14**：Jingren Zhou, Per-Åke Larson, Ronnie Chaiken. *Incorporating Partitioning and Parallel Plans into the SCOPE Optimizer*. IEEE ICDE 2010. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/incorporating-partitioning-and-parallel-plans-into-the-scope-optimizer.pdf)
7. **#06**：Laura M. Haas, Michael J. Carey, Miron Livny. *Seeking the Truth About Ad Hoc Join Costs*. Technical Report #1148, May 1993 (VLDB Journal 1997). [原始 PDF（共 27 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/cost-model/seeking-the-truth-about-ad-hoc-join-costs.pdf)

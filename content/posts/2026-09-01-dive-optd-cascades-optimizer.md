---
title: "【源码】从零实现一个 Cascades Optimizer：optd 全链路源码剖析"
date: 2026-09-01T00:00:00+08:00
lastmod: 2026-10-18T00:00:00+08:00
slug: "dive-optd-cascades-optimizer"
categories:
  - 数据库
tags:
  - optd
  - Cascades Optimizer
  - 查询优化器
  - Memo
  - DataFusion
  - CBO
description: "从亲手实现一个现代 Cascades Optimizer 的视角，结合 optd 源码，沿着一条 SQL 从接入、Memo 去重、规则匹配、异步递归搜索、手算 Winner 到自适应反馈与 DataFusion 执行计划落地的完整链路拆解核心机制。"
draft: false
toc: true
math: true
---

> 本系列第 3/4 篇 · 优化器 / Cascades · [上一篇]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}}) · [下一篇]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}})

如果让你从零写一个查询优化器（Query Optimizer），最符合直觉的做法是什么？大概是拿到语法解析器吐出的逻辑计划树，写十几条自顶向下的树改写函数：遇到过滤条件就往下推，遇到两表连接就把小表换到左边，最后把每个逻辑节点逐个替换成物理算子。

只要查询稍微复杂一点，这套“在单棵树上原地修剪”的朴素方案马上就会在三个地方撞墙。不妨看一条最简单的三表内连接（Inner Join）：

```sql
SELECT *
FROM a
JOIN b ON a.k = b.k
JOIN c ON b.k = c.k;
```

解析器最初给出的通常是一棵左深树 `Join(Join(Scan(a), Scan(b)), Scan(c))`。但想挑出最快计划，我们必须面对连接重排（Join Reordering）与物理算子选择的乘积爆炸：

1. **拷贝整棵树会让内存与搜索量指数级膨胀**：仅仅三个表的连接，就有 `Join(Scan(a), Join(Scan(b), Scan(c)))`、`Join(Scan(c), Join(Scan(b), Scan(a)))` 等多种树形；每个逻辑 `Join` 又可以落成 Nested Loop Join、Hash Join 或 Merge Join。如果每次尝试新顺序都克隆整棵计划树，`Scan(a)` 和 `Join(b, c)` 这样的公共子树会被重复构造和遍历成百上千次。
2. **局部最优子计划无法跨分支共享**：当你费尽周折算清楚 `b ⋈ c` 用 Hash Join 最便宜、输出约 500 行之后，外层无论拿 `a` 从左边连它还是从右边连它，本来都该直接复用这个结论，而不是换个父节点就推倒重算。
3. **单向覆盖式改写会过早毁掉更优候选**：消除空投影或常量折叠永远稳赚不赔，可以直接覆盖原树；但交换连接顺序、或者跨连接下推过滤条件到底划不划算，在底层物理算子计价完成之前谁也不知道。一旦提前覆盖了原树，原本更便宜的物理组合就彻底进不了视野。

Cascades 框架之所以成为现代分析型数据库优化器的事实标准，正是因为它正面回答了这三个矛盾：**不再问“怎样把眼前这棵树改得更好”，而是把所有语义等价的表达式压缩进同一个等价类（`Group`），让父算子只引用子 `Group` 编号而非具体子树，再通过动态规划（Dynamic Programming）复用子问题最优解（`Winner`），并用已知最优代价作为上界进行分支限界（Branch-and-Bound）剪枝。**

想真正吃透这套机制，直接一头扎进几十万行代码的工业级优化器很容易迷失在琐碎的工程防御里。CMU 数据库组开源的 [optd](https://github.com/cmu-db/optd) 恰好提供了一个难得的解剖样本：它的代码量远小于生产数据库，却把计划表示、Memo 去重与合并、逻辑与物理规则、递归任务搜索、代价与统计推导、两阶段预算控制、运行时基数反馈以及 DataFusion 执行计划桥接完整跑通了；同时，它在物理属性搜索上的未竟之处（例如单 Winner 槽位与尚未接通 Cascades 主链的 `Required Properties`），又恰好标出了从“单机教学原型”迈向 [StarRocks 分布式生产优化器]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}}) 必须跨过的那道门槛。

下面我们就顺着一条 SQL 进入和离开 `optd` 的真实生命周期，把一个现代 Cascades 优化器的最小正确内核拆开看清。

---

## 一、接入边界：如何在不重写 SQL 引擎的前提下独占规划权

从零实现一个通用优化器，最明智的工程切法不是自己去写词法解析、Catalog 元数据管理和向量化算子，而是借壳成熟的查询引擎，只把“从关系代数（Relational Algebra）到物理执行计划”这一段核心搜索接管过来。`optd` 选择了 Apache DataFusion 作为宿主，并将代码干净地切成了三层：

```text
DataFusion SQL / LogicalPlan
          │
          ▼
optd-datafusion-bridge
  into_optd.rs         DataFusion LogicalPlan → optd PlanNode（位置列索引归一化）
          │
          ▼
optd-datafusion-repr
  plan_nodes.rs        DataFusion 领域算子（Scan/Join/Agg...）与标量谓词类型
  rules/               逻辑变换（Transformation）与物理实现（Implementation）规则
  properties/          Schema、ColumnRef 等逻辑属性推导
  cost/                基础代价模型与运行时自适应代价模型
          │
          ▼
optd-core
  nodes.rs             与宿主无关的通用 PlanNode / PredNode 表示
  cascades/memo.rs     Group、MemoPlanNode、结构去重与 Group Merge
  cascades/tasks2.rs   任务调度、规则触发、上下界剪枝与 Winner 维护
  cost.rs              CostModel 抽象接口
          │
          ▼
optd-datafusion-bridge
  from_optd.rs         optd Physical Plan → DataFusion ExecutionPlan
  physical_collector   注入 CollectorExec 采集运行时真实行数
```

仔细看这个依赖方向：`optd-core` 完全不知道世界上存在 `TableScan`、`HashJoin` 或是 DataFusion 的任何数据结构，它只要求外部传入的 `NodeType` 能区分“逻辑算子”和“物理算子”。领域算子语义、规则集和代价公式全部收拢在 `optd-datafusion-repr` 中，而与 DataFusion 内部类型的脏活翻译则隔离在最外层的 `optd-datafusion-bridge` 里。

要把规划权真正握在自己手里，接入层必须干脆利落地关掉宿主自带的优化器。翻开 `optd-datafusion-bridge/src/lib.rs` 里的 `create_df_context`：

```rust
if !use_df_logical {
    session_config.options_mut().optimizer.max_passes = 0;
    builder = builder.with_optimizer_rules(vec![]);
}

builder = builder.with_physical_optimizer_rules(vec![]);
builder = builder.with_query_planner(optimizer.clone());
```

当 `use_df_logical = false` 时，DataFusion 原生的逻辑改写轮次被清零，物理优化规则更是无条件清空，最后将 `OptdQueryPlanner` 注册为唯一的物理规划器。这样在看 `EXPLAIN` 时，所有的连接顺序调整、物理算子拍板和代价估算都百分之百来自 `optd`，绝不会出现两套优化器互相干扰的罗生门；而遇到 DML、DDL 等 `optd` 暂未覆盖的顶层语句时，`OptdQueryPlanner` 又会稳妥地回退给 DataFusion 的 `DefaultPhysicalPlanner`，不会把 `unimplemented!` 直接砸给用户。

进入 `OptdQueryPlanner::create_physical_plan_inner` 后，一条查询的主干流水线只有四步：

```rust
let mut optd_rel = ctx.conv_into_optd(logical_plan)?;

if optimizer.is_heuristic_enabled() {
    optd_rel = optimizer.heuristic_optimize(optd_rel);
}

let (group_id, optimized_rel, meta) =
    optimizer.cascades_optimize(optd_rel)?;

let physical_plan =
    ctx.conv_from_optd(optimized_rel, meta).await?;
```

看到这里你可能会问：既然要做基于代价的 Cascades 搜索，为什么进 Memo 之前还要先跑一轮启发式优化（`heuristic_optimize`）？

查看 `DatafusionOptimizer::default_heuristic_rules` 就能明白其中的分寸感：这一阶段只做**无条件正收益的确定性归约**——比如消除空的 `Project` / `Filter` / `Join` / `Limit`、合并相邻投影与过滤、消除重复的排序或聚合表达式，以及将子查询产生的 `RawDependentJoin` 解关联（Decorrelate）下推成常规连接。把这些语法噪声和相关子查询在进 Memo 前清理干净，既免去了在 Cascades 里写复杂相关子查询规则的负担，又防止大量毫无比较价值的冗余变形污染等价类空间；至于真正改变中间结果基数、需要结合物理算子算总账的 Filter Pushdown 和 Join Reorder，则原封不动留给后面的 Cascades 搜索。

在 `into_optd.rs` 把 DataFusion `LogicalPlan` 翻译成 `optd` 内部节点的过程中，最关键的设计不是枚举映射，而是**把按名字引用的列统一改写成稳定的位置索引（Positional Index）**：

| DataFusion 节点 | optd 节点 | 边界翻译的关键动作 |
| --- | --- | --- |
| `TableScan` | `LogicalScan` | 保存 `TableSource`，自带投影拆成显式的上层 `LogicalProjection` |
| `Projection` | `LogicalProjection` | 标量表达式转 `PredNode`；若含子查询则先挂接 `RawDependentJoin` |
| `Filter` | `LogicalFilter` | 列名解析为输入 Schema 的位置下标，提取子查询 |
| `Join` | `LogicalJoin` | 左孩子占 `[0, left_width)`，右孩子列下标整体偏移 `+ left_width` |
| `Aggregate` | `LogicalAgg` | 拆分为聚合函数列表与分组键表达式列表 |
| `Sort` / `Limit` | `LogicalSort` / `LogicalLimit` | 排序方向、`skip` / `fetch` 参数统一编码进 `predicates` |
| `SubqueryAlias` | 透明穿过 | 内部已全部改用位置下标，无需保留表别名包装 |
| `EmptyRelation` | `LogicalEmptyRelation` | 记录输出 Schema 及是否产出单行占位数据 |

一旦确立了“左输入占 `[0, left_width)`、右输入从 `left_width` 起算”的位置契约，后面所有连接交换律和结合律规则在挪动子树时，就必须严丝合缝地同步改写谓词里的列偏移量并维护输出列顺序——这也正是写优化器规则最容易埋下静默错数 Bug 的地方。

---

## 二、Memo 的状态契约：为什么关系子树与标量谓词必须分家

走完前置归约，计划正式进入 `optd-core`。早期 `optd` 设计文档曾把关系算子和标量表达式混用同一套 `RelNode`，但在当前源码 `optd-core/src/nodes.rs` 中，两者被明确拆成了两套结构：

```rust
pub enum PlanNodeOrGroup<T> {
    PlanNode(ArcPlanNode<T>),
    Group(GroupId),
}

pub struct PlanNode<T: NodeType> {
    pub typ: T,
    pub children: Vec<PlanNodeOrGroup<T>>,
    pub predicates: Vec<ArcPredNode<T>>,
}

pub struct PredNode<T: NodeType> {
    pub typ: T::PredType,
    pub children: Vec<ArcPredNode<T>>,
    pub data: Option<Value>,
}
```

为什么要把 `children`（关系输入）和 `predicates`（标量谓词参数）分家？因为 Memo 的核心使命是压缩**关系子问题**的组合空间。`Scan`、`Join`、`Agg` 这类关系算子改变行集与输出模式，值得建立 `Group` 去追踪不同连接次序和物理实现；而 `a.k = b.k`、`x + 1`、`SortOrder(Asc)` 这类标量树只是算子的附带参数。如果把每一个常量和加减号都塞进关系 `Group`，不仅规则匹配器很容易把标量子树误认为关系输入，Memo 的状态表也会被细碎表达式冲垮。分家之后，关系边沿 `PlanNode.children` 传播 Schema 与基数，标量树通过哈希驻留（Interning）拿到唯一的 `PredId` 供多个算子轻量共享——代价则是标量表达式自身不参与 Cascades 等价搜索，常量折叠与布尔化简得靠前置规则或执行器搞定。

当一棵完整的 `PlanNode` 被塞进 `optd-core/src/cascades/memo.rs` 时，它会被拆解成经典的 `MExpr`（源码中叫 `MemoPlanNode`）：

```rust
pub struct MemoPlanNode<T: NodeType> {
    pub typ: T,
    pub children: Vec<GroupId>,
    pub predicates: Vec<PredId>,
}
```

注意看它的字段：`children` 里存的不再是具体的子算子，而是 **`GroupId`**！这就引出了 Memo 里最核心的三个身份标识：

- **`GroupId`（语义等价类）**：代表“输出关系语义完全相同的一组候选表达式”；
- **`ExprId`（具体算子表达式）**：代表某个挂着子 `GroupId` 列表和 `PredId` 列表的具体 `MemoPlanNode`；
- **`PredId`（驻留标量参数）**：代表去重后的过滤条件、投影列或排序键。

举个源码测试里的真实快照，同一个 `Group !31` 里会同时躺着逻辑变换产物和物理实现产物：

```text
Group !31
  expr 8  : Filter(!6, #0 = #2)                             // 笛卡尔积 + 过滤
  expr 15 : Join(Inner, !2, !2, #0 = #2)                    // 逻辑内连接
  expr 25 : PhysicalNestedLoopJoin(!2, !2, #0 = #2)         // 物理嵌套循环连接
  expr 28 : PhysicalHashJoin(!2, !2, left=[#0], right=[#0]) // 物理哈希连接
```

这四条表达式结构各异，但吐出的关系结果集与列定义分毫不差，因此共享同一个 `Group !31`；只不过前两条是逻辑跳板，只有后两条物理表达式才有资格竞选最终的 `Winner`。

要把这套共享图维护得滴水不漏，`NaiveMemo` 必须守住三条铁律：

1. **自底向上插入与结构去重（`add_new_group_expr_inner`）**：插入一棵新树时，必须先递归把所有子 `PlanNode` 送进 Memo 拿到规范化的 `children_group_ids`，再将标量参数驻留为 `pred_ids`，拼出 `MemoPlanNode` 后去查哈希表 `expr_node_to_expr_id`。只要算子类型、子 `GroupId` 序列和 `PredId` 序列完全一致，无论它是哪条规则绕了多大弯路推导出来的，都会直接命中同一个 `ExprId`，绝不重复占据搜索空间。
2. **等价规则产物回填原 Group（`add_expr_to_group`）**：当结合律把 `(A ⋈ B) ⋈ C` 改写成 `A ⋈ (B ⋈ C)` 时，新树底下的 `B ⋈ C` 会新建或命中一个子 Group，但新树的根节点 `A ⋈ (...)` 必须通过 `self.optimizer.add_expr_to_group(expr, group_id)` 插回旧根所在的那个 `Group`。这条调用正是规则系统的契约底线：**规则返回的不是替代品，而是“同一关系语义的另一种写法”。**
3. **自底向上的级联合并（`merge_group_inner`）**：设想两个原本独立的 `Group` 在后续规则演化中推导出了一模一样的 `MemoPlanNode`——这说明两个 `Group` 其实语义等价！此时 `merge_group_inner` 会把源 Group 的全部 `ExprId` 并入目标 Group、保留代价更优的 `Winner`、记录重定向映射，并遍历父表达式把引用到的旧子 `GroupId` 替换为新 ID；一旦替换后导致某两个父 `MemoPlanNode` 也撞了衫，合并就会顺着父指针一路向上级联传播。（`optd` 将其命名为 `NaiveMemo`，是因为目前仍靠全表扫描重写父引用；工业界系统通常会在这里换上并查集 Union-Find 与反向父索引 `Parent Index`。）

正因为同一个 `Group` 内的所有表达式语义严格等价，**逻辑属性（Logical Property）天然属于 `Group` 而非单个 `MExpr`**。在 `Group` 创建瞬间，`optd` 只拿第一条种子表达式跑一次推导，就把结果固化在 `Group.properties` 上：

- **`SchemaPropertyBuilder`**：锁定输出列名、数据类型与可空性（Nullability）；
- **`ColumnRefPropertyBuilder`**：追踪每个输出槽位源自哪张基表的哪一列（Column Lineage），并用并查集维护等值连接建立的列等价集合（例如由 `a.k = b.k` 和 `b.k = c.k` 自动打通 `{a.k, b.k, c.k}`，供高级代价模型挑选连接统计信息）。

这也顺手送给了自研优化器一把极佳的排错标尺：在 Debug 模式下，每当一条新表达式并入已有 `Group`，只要重新算一遍它的 `Schema` 和列血缘并与 `Group` 原有属性比对——一旦对不上，百分之百是刚才触发的那条规则写坏了列索引或可空性，当场断言报错，根本不用等到跑出错误查询结果再去大海捞针。

---

## 三、规则系统：模式匹配只是敲门砖，重写列索引才是脏活

有了 Memo 做容器，接下来就要靠规则（`Rule`）往里面源源不断地注入等价候选。`optd` 用一个极简的 Trait 统一了两类规则：

```rust
pub trait Rule<T, O> {
    fn matcher(&self) -> &RuleMatcher<T>;
    fn apply(&self, optimizer: &O, binding: ArcPlanNode<T>) -> Vec<PlanNodeOrGroup<T>>;
    fn name(&self) -> &'static str;
    fn is_impl_rule(&self) -> bool { false }
}
```

`is_impl_rule()` 区分了优化器里职责截然不同的两支部队：

- **逻辑变换规则（Transformation Rule，默认 `false`）**：输入逻辑算子，输出等价的逻辑算子（如连接交换律、连接结合律、过滤下推），负责横向拓宽等价搜索空间；
- **物理实现规则（Implementation Rule，`true`）**：输入逻辑算子，输出可执行的物理算子（如把逻辑 `Join` 翻译成 `PhysicalHashJoin` 或 `PhysicalNestedLoopJoin`），负责把抽象语义落地成能算代价的物理方案。

把上一节的自底向上哈希去重（Hash-Consing）与这里的双轨规则放到同一张图里，就能看清一棵初始 `PlanNode` 树是如何被压缩进 `Group` 等价类，又如何在同一等价组内分化出逻辑跳板与物理候选的：

![optd Memo 自底向上哈希去重与双轨规则演化](/images/diagrams/opt-optd-memo-dedup.svg)

*图 1：标量谓词驻留为 `PredId`，关系子树自底向上归一化为子 `GroupId` 并在 `expr_node_to_expr_id` 中去重；`Transformation` 规则将等价逻辑写法插回原 `Group`，`Implementation` 规则则产出参与 `Winner` 竞选的物理 `GroupExpr`。*

为什么要刻意拆开？试想某条两层规则想检查左孩子是不是一个 `Inner Join`（例如结合律 `JoinAssocRule`）：

```rust
define_rule!(
    JoinAssocRule,
    apply_join_assoc,
    (Join(JoinType::Inner), (Join(JoinType::Inner), a, b), c)
);
```

此时父任务只需要让左孩子所在的子 `Group` 把隐藏的逻辑 `Inner Join` 形态探索出来（只跑 Transformation Rule），完全没必要把左孩子底下的几十种物理 Hash Join / Sort 组合提前实例化一遍。

当 `RuleMatcher` 去 Memo 里抓取匹配子树（`Binding`）时，面临的情况比普通树匹配复杂得多：根 `MemoPlanNode` 的左孩子是一个 `GroupId`，而那个子 `Group` 里可能同时躺着 5 个不同的 `Inner Join` 表达式！于是匹配器必须钻进子 `Group` 逐一匹配内部节点，遇到 `Any` 通配符时保留 `Group(GroupId)` 占位符，最后把多个孩子的匹配结果做**笛卡尔积**，还原出一批完整的 `ArcPlanNode<T>` 喂给 `Rule::apply`。你可以立刻看出这里的性能暗礁：如果左子 Group 有 15 个匹配、右子 Group 也有 15 个匹配，单次规则触发就会瞬间物化出 `15 × 15 = 225` 个 Binding（`optd` 在单次产出满 200 个 Binding 时会专门打一条告警日志；生产级内核往往必须在这里改成按需流式迭代器并加上规则优先级 `Promise` 限流）。

不过，真正考验规则作者基本功的还不是匹配器，而是 `apply` 函数里的语义守恒。我们翻开 `optd-datafusion-repr/src/rules/joins.rs`，看三条最典型的规则是怎么跨过陷阱的：

1. **`JoinCommute`（交换律：换了左右孩子，别忘了补一层投影归位）**
   假设左表 `A` 宽 `a_width` 列，右表 `B` 宽 `b_width` 列。如果只是把 `Join(A, B)` 翻成 `Join(B, A)`，输出列顺序立刻变成了 `[B..., A...]`，直接违背原 `Group` 的 Schema 契约！因此 `JoinCommute` 必须连做三件事：先把连接条件里属于 `A` 的列下标 `i` 改写成 `b_width + i`、属于 `B` 的列下标 `j` 改写成 `j - a_width`，生成交换后的 `Join(B, A)`，**最后在头顶套上一层 `LogicalProjection` 把输出列顺序重新拨回 `[A..., B...]`**（这层过渡投影后续会由 `ProjectionPullUpJoin` 或 `ProjectMergeRule` 顺手消掉）。
2. **`JoinAssoc`（结合律：谓词越界引用的连接绝不能硬拆）**
   想把 `(A ⋈ B on cond1) ⋈ C on cond2` 改写成 `A ⋈ (B ⋈ C on cond2') on cond1`，前提是外层条件 `cond2` **只引用了 `B` 和 `C` 的列**。源码通过 `rewrite_column_refs` 尝试把 `cond2` 里的列索引全部减去 `A` 的宽度：一旦发现某个列索引落在 `[0, a_width)` 区间（说明 `cond2` 依赖左表 `A`，比如 `a.x + b.y = c.z`），函数立刻返回 `None`，结合律当场放弃。同时该规则严格限定 `JoinType::Inner`——因为一旦换成 `Left Outer Join`，补 `NULL` 侧的位置和谓词拒绝空值（Null-Rejecting）条件就会彻底改写等价性，绝不能照搬内连接代数定律。
3. **`HashJoinRule`（物理实现：算子能力边界检查与保底回退）**
   并不是所有逻辑连接都能落成哈希连接。当前 `HashJoinRule` 会逐项核验：必须是 `Inner Join`、连接条件必须是单个等值或一串 `AND` 相连的等值比较、等号两侧必须直接是分别来自左右输入的 `ColumnRef`。核验通过后，它把全局位置下标拆回左右表各自的局部列号，吐出 `PhysicalHashJoin(left_group, right_group, left_keys, right_keys)`；万一遇到非等值条件 `a.x < b.y`，`HashJoinRule` 安静返回空集，由兜底的通用转换规则将其落成 `PhysicalNestedLoopJoin`，确保查询不会因为高阶算子挑食而报错挂掉。

---

## 四、搜索与计价闭环：递归任务流、上下界剪枝与手算 Winner

现在等价容器（Memo）和候选制造机（Rules）都就位了，谁来指挥它们既不死循环、又不漏掉好计划，还能尽早剪掉烂分支？

经典 Cascades 论文通常画一个显式的任务栈（Task Stack），压入 `OptimizeGroup`、`OptimizeExpr`、`ExploreGroup`、`ApplyRule`、`OptimizeInputs` 五类对象循环弹出。打开 `optd-core/src/cascades/tasks2.rs` 会看到一个很有意思的工程实现：它保留了完全一致的任务切分语义，但用一组**相互递归的 Rust `async fn`** 代替了手写状态机栈帧——注意这里的 `async` 并不是为了多线程并发搜索，而是为了方便穿插状态控制；入口 `fire_optimize_tasks` 会先调 `stacker::grow` 扩出一块 32 MB 的充裕调用栈防止深层递归爆栈，再用 `pollster` 单线程同步跑完整个 Future。

把这套递归调用链摊开，一次 `OptimizeGroup` 从防环守卫、存量物理候选优先估价、规则展开到 `Winner` 记忆化与超预算剪枝的完整协作流如下：

![optd tasks2.rs 任务调度、Winner 记忆化与分支限界剪枝](/images/diagrams/opt-optd-task-stack.svg)

*图 2：`tasks2.rs` 任务流全貌——组内物理候选率先执行 `OptimizeInputsTask` 建立有限 `UpperBound`，`in_progress` 与 `fired_rules` 阻断自引用及 `A⋈B ↔ B⋈A` 环路，超预算分支当场截断（可打开[交互式任务流视图](/diagrams/opt-optd-task-stack.html)分阶段高亮查看）。*

这段流程里藏着 Cascades 区别于“先跑完全部逻辑枚举、再统一转物理计划”的两大精髓。

第一，**逻辑扩张与物理计价是交错进行的，而且物理候选永远优先估价**。只要组里已经有一条保底物理方案，立刻调 `OptimizeInput` 算出它的总成本并设为当前 `upper_bound`；后续规则每吐出一条新的物理方案，马上跟进估价，一旦更便宜就继续收紧上界，用实打实的成本天花板把后面昂贵的搜索分支扼杀在摇篮里。

第二，**去重靠 Memo，防重入靠 `fired_rules`**。如果只靠 Memo 结构去重而不记 `fired_rules: HashMap<ExprId, HashSet<RuleId>>`，连接交换律就会在 `expr 1: A ⋈ B` 和 `expr 2: B ⋈ A` 之间永无休止地互相匹配、互相生成已存在的对方。显式记录每个 `(ExprId, RuleId)` 组合只触发一次，搜索图才真正具备有限收敛保证。

再往里看最核心的计价函数 `OptimizeInput`，它把**自顶向下的预算收紧**和**自底向上的动态规划**拧在了同一个循环里。假设某个物理算子有多个子 Group 需要依次求最优解，传给第 `i` 个孩子的预算上界为：

$$\text{UpperBound}_{\text{child}_i} = \text{UpperBound}_{\text{parent}} - \text{Cost}_{\text{so\_far}} + \text{Cost}_{\text{current\_child}_i}$$

只要累加过程中的已知开销突破了 `UpperBound_parent`，当前物理算子当场被剪枝放弃（目前 `optd` 尚未把“还没开始算的兄弟孩子的成本下界 Lower Bound”加进 `Cost_so_far`，暂按 `0` 保守处理——这意味着它可能会少剪掉几个分支，但绝不会误杀真正的最优解）。当所有子 Group 都顺利拿到各自的 `Winner` 后，父节点调用 `optd-core/src/cost.rs` 里的两个正交接口收尾：

1. **`derive_statistics`**：根据子节点的统计信息推导当前算子的输出行数等统计量，向上汇报数据规模；
2. **`compute_operation_cost`**：计算当前算子自身的局部资源开销，再加上所有子 Winner 的累计代价得到 `total_cost`。若加权标量代价 `weighted_cost` 低于当前 Group 的擂主，就更新 `WinnerInfo`（同时存下 `expr_id`、局部代价、累计代价和 `Arc<Statistics>`）。

光看接口太抽象，我们直接拿仓库自带的测试 `optd-sqlplannertest/tests/utils/memo_dump.planner.sql` 来**手算一次真实的 Winner 诞生过程**。查询是一条带排序的自连接：

```sql
SELECT *
FROM t1 AS a, t1 AS b
WHERE a.t1v1 = b.t1v1
ORDER BY a.t1v1;
```

对照 `optd-datafusion-repr/src/cost/base_cost.rs` 的二维基础代价模型（`Cost = [compute, io]`，`weighted_cost = compute + io`）：

| 物理算子 | 输出行数估算（`derive_statistics`） | 局部算子代价（`compute_operation_cost`） |
| --- | --- | --- |
| `PhysicalScan` | 表统计行数（缺省 `1000`） | `io = rows` |
| `PhysicalFilter` | `max(1, child × 0.01)` | `compute = child_rows × pred_cost` |
| `PhysicalNestedLoopJoin` | `left × right × 0.01` | `compute = left × right × pred_cost + left` |
| `PhysicalHashJoin` | `min(left, right)` | `compute = 2 × left + right` |
| `PhysicalSort` | 与输入行数相同 | `compute = rows × ln(1 + rows)` |

我们跟着 Memo Trace 一笔笔核对账单：

1. **叶子扫描复用**：两个表别名 `a` 和 `b` 扫的是同一张基表 `t1`，结构去重后直接共享同一个 `Group !2`。缺省表行数为 `1000`，单次扫描局部代价为 `{compute: 0, io: 1000}`，两侧输入合计贡献 `2000` 点 I/O 代价。
2. **连接等价类打擂（`Group !31`）**：这里汇聚了 `Filter + CrossJoin`、逻辑 `InnerJoin`、`PhysicalNestedLoopJoin` 和 `PhysicalHashJoin`。先算嵌套循环连接：局部计算量高达 `1000 × 1000` 量级，加上双侧扫描后总加权代价飙到 **`1,003,000`**；再算哈希连接：局部计算代价为 `2 × 1000 + 1000 = 3000`，叠加上双侧扫描的 `2000` I/O，总加权代价仅 **`5000`**！`Group !31` 的擂主毫无悬念归属 `PhysicalHashJoin`，输出基数按 `min(1000, 1000)` 估为 `1000` 行。
3. **根节点排序封顶**：根 Group 的 `PhysicalSort` 接收连接输出的 `1000` 行，局部代价为 `1000 × ln(1001) ≈ 6908.75`，加上子 Winner 的 `5000`，最终根 Group Winner 的总加权代价定格在 **`11908.75`**，提取出的物理树正是 `PhysicalSort → PhysicalHashJoin → (PhysicalScan, PhysicalScan)`。

这套简单到能用纸笔算清的基础公式当然代表不了真实磁盘和 CPU 耗时（比如固定 1% 的过滤选择率就非常粗糙），但在自研优化器起步阶段，**“能手算核对每一分钱的来龙去脉”比一上来堆砌直方图重要十倍**——只有先确保搜索骨架、去重合并与动态规划账本分毫不差，后续替换高精度的统计与代价模块才不会陷入盲人摸象。

---

## 五、搜索预算、物理属性断层与运行时自适应反馈

把玩具级 SQL 跑通之后，一旦放进多表复杂查询或真实执行环境，优化器马上会撞上三个更深层的系统问题：搜索超时怎么办？父算子要求输入有序或按哈希分布怎么办？统计信息估错了怎么补救？我们看看 `optd` 当前源码在这三道关卡上分别走到了哪一步。

### 5.1 两阶段优化与降级预算：宁可少搜一半，绝不两手空空

连接交换律和结合律一旦全开，`N` 张表的连接空间会以指数级膨胀。如果在还没摸出一个完整物理计划之前就把时间全耗在逻辑变换上，一旦规划超时，优化器连个能跑的保底计划都交不出来。

为此，`DatafusionOptimizer::cascades_optimize` 把每次优化拆成了稳扎稳打的两步：

- **Stage 1（锁定基线）**：临时禁用 `JoinCommute` 和 `JoinAssoc`，按原始连接顺序快速跑完一轮搜索，第一时间拿到一棵合法的物理 Winner 和有限的成本上界；
- **Stage 2（放开重排）**：重新启用连接交换与结合规则，调用 `step_next_stage` 清空 `explored_group` 与 `explored_expr` 标记，但**完整保留 Memo、已有 Winner 和 `fired_rules`**，在 Stage 1 建立的成本上界保护下展开连接重排搜索。

与此同时，源码设置了两道硬预算防线：当 Memo 表达式总数突破空间预算（默认 `partial_explore_space = 1 << 14`）时，立即停掉所有扩张空间的 Transformation Rule，只允许 Implementation Rule 把已有逻辑节点落地成物理算子；当任务步数突破迭代预算（默认 `partial_explore_iter = 1 << 18`）时，停止继续展开规则，只要根 Group 已有 Winner 就尽快收工返回。

阅读这部分源码时，有两处微妙的状态细节值得自研者引以为戒：一是当前的 `explored_group` 只拿裸 `GroupId` 当键，没有带上当时的 `UpperBound`——理论上如果某个子 Group 第一次在极严苛的上界下因剪枝空手而归，后来另一个父分支带着更宽松的上界再来访问它时，不该直接复用“已探索完毕”的旧标记；二是 `step_clear` 重置查询时重建了 Memo，却没把 `OptimizerContext` 里的预算耗尽标志位一起清零，长生命周期实例连续处理多条查询时需要把单次搜索状态与跨查询状态彻底剥离。

### 5.2 核心断层：已有接口却尚未入场的 `Required Properties`

翻开 `optd-core/src/physical_property.rs`，你会看到一套设计得相当工整的物理属性抽象：`derive`（自底向上推导输出属性）、`passthrough`（自顶向下把父要求传给孩子）、`satisfies`（检查现有属性能否满足要求）以及 `enforce`（不满足时插入 Sort 或 Exchange 补偿算子）。

然而一进到 Cascades 搜索主链，就会发现：

```rust
fn optimize_with_required_props(...) {
    unimplemented!()
}
```

当前的 DataFusion Cascades 构造器还没有注册物理属性构建器，`GroupInfo` 里也只存了一个孤零零的 `winner: Winner`，这意味着 `optd` 实际求解的子目标退化成了 `Optimize(GroupId, UpperBound)`，缺少了最关键的第二个维度 `RequiredProperties`。

少掉这一个维度会卡住什么？在单机引擎里，父算子 `MergeJoin` 或 `ORDER BY` 无法向子 Group 发出“请给我按 `a` 列升序的最优计划”的请求，从而无法让“自带索引有序但略慢的扫描”与“无序快扫 + 显式 Sort 算子”同台比价；到了分布式 MPP 引擎里，问题更加致命——翻看 `optd-datafusion-bridge/src/from_optd.rs` 把物理节点翻译成 DataFusion `ExecutionPlan` 的代码，会发现 `PhysicalHashJoin` 硬编码了单一分区模式：

```rust
PartitionMode::CollectLeft
```

也就是说，无论右表多小、左表多大，或者底层数据是否已经按连接键哈希分桶，搜索内核都看不见 Broadcast 与 Shuffle Exchange 的网络账单。要把 `optd` 推向分布式生产环境，头号手术绝不是再写二十条改写规则，而是把物理属性集塞进状态表的每一个角落：

```rust
#[derive(Hash, Eq, PartialEq)]
struct WinnerKey {
    group_id: GroupId,
    required: PhysicalPropertySet, // 例如 Ordering + Distribution
}

struct GroupInfo {
    winners: HashMap<PhysicalPropertySet, Winner>, // 每个物理要求各自维护最优擂主
}
```

只要 `GroupInfo` 升级成按 `PhysicalPropertySet` 索引的多维账本，任务去重键、`OptimizeInput` 的属性下推与 `Enforcer` 补偿计价就能顺理成章地闭环——这正是我们在[下一篇 StarRocks 优化器剖析]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}})中要重点拆解的生产级主战场。

### 5.3 自适应反馈：留住千辛万苦搜出的 Memo，只推翻旧 Winner

除了静态规则搜索，`optd` 还演示了一条非常精巧的运行时自适应反馈（Adaptive Feedback）回路。

当开启自适应模式时，桥接层 `from_optd.rs` 在构造 DataFusion 物理执行树的同时，通过 `PlanNodeMetaMap` 把每个物理节点关联回它所属的 Memo `GroupId`，并在外层套上一个轻量探针算子 `CollectorExec`。查询真正跑起来后，`CollectorReader` 一边流式转发 `RecordBatch`，一边数出真实流过的行数，流结束时写回共享存储 `RuntimeAdaptionStorage[group_id] = (actual_row_count, iteration)`。

妙处发生在下一轮规划时。普通模式下每次新查询都会调 `step_clear` 把 Memo 连根清空；而自适应模式调用的是 **`step_clear_winner`**，与 DataFusion 执行期的 `CollectorExec` 探针串起一条“保图重算”的反馈回路：

![optd + DataFusion 运行时基数采集与保留 Memo 重寻优闭环](/images/diagrams/opt-optd-adaptive-feedback.svg)

*图 3：`from_optd.rs` 将物理节点包装为 `CollectorExec`，在流式转发数据的同时按 `GroupId` 把实测行数写回 `AdaptionStorage`；下一轮经 `step_clear_winner()` 保留已展开的全部 `MExpr` 与 `fired_rules`，由 `AdaptiveCostModel` 在原等价图上直接重算出新 `Winner`。*

这个设计一针见血地切开了**“等价空间结构”与“统计代价评判”**：既然 SQL 没变、代数等价定律也没变，上一轮花大力气枚举出来的连接顺序和物理候选完全合法，犯错的只是基数估计；那就把旧擂主撤掉，带着真实基数在原 Memo 上重算一遍成本即可！当然，目前这还只是一个单机教学级闭环——`AdaptiveCostModel` 仅在 `PhysicalScan` 节点消费反馈行数、`CollectorExec` 只采集 `partition == 0` 的单分区计数，而且 `GroupId` 只在单实例内存生命周期内有效；要想做成跨会话、跨集群的历史反馈优化（History-Based Optimization，HBO），还必须把键升级为包含表结构与统计版本号的规范化子计划指纹。

---

## 六、从零自研与源码走读路线图

拆完了 `optd` 的五脏六腑，如果你也打算亲手写一个玩具乃至生产级 Cascades 优化器，千万不要一上来就拉几十个算子和直方图统计——那样一旦选错计划，你根本分不清是规则写毁了语义、Memo 漏合了等价类，还是剪枝误杀了候选。最稳妥的推进节奏是分八小步逐层验明正身：

1. **最小代数骨架**：只定义 `Scan`、`Filter`、`Join` 三个算子和等值合取谓词，接在 DataFusion 或内存表前端后面，把位置列索引和哈希判等写稳；
2. **Memo 驻留与合并**：实现 `MemoPlanNode` 去重与 `Group Merge`，写死三条单测不变量（同名表扫描唯一、重复插入返回同 `ExprId`、子 Group 合并自动触发重复父节点级联合并）；
3. **单条变换规则 + 逻辑属性断言**：只写一条 `Inner Join Commute`，配齐列下标偏移与顶层归位 `Projection`，并在 Debug 模式下强制校验新表达式的 `Schema` 与列血缘必须严格等于原 `Group`；
4. **最小物理实现与纸笔代价**：加入 `PhysicalScan`、`NLJ`、`HashJoin`，用最简单的行数线性/乘积公式计价，确保每一条双表、三表连接都能手算出与程序一致的 Winner；
5. **递归搜索与无损剪枝对拍**：跑通 `OptimizeGroup / Expr / Inputs` 与 `fired_rules` 后，再打开 `UpperBound` 分支限界，并用随机查询对拍“开剪枝 vs 关剪枝”的最终最优代价，必须一分不差；
6. **两阶段预算降级**：引入 Stage 1 基线保底与空间/步数双预算，并在 `EXPLAIN` 中显式打印是否触发了截断，防止把“预算耗尽提前收工”误判成代价模型失灵；
7. **接入单机排序属性（`RequiredOrdering`）**：把 `GroupInfo` 升级为 `HashMap<RequiredOrdering, Winner>`，让 `Sort` 作为 `Enforcer` 计入总账，验证同一 Group 在“要求无序”与“要求按 `k` 有序”时能选出不同 Winner；
8. **扩展分布式属性与高阶统计**：最后再去引入 `Hash / Broadcast / Single` 分布契约、网络交换代价、NDV/直方图估算以及带稳定签名的运行时基数反馈。

如果你想直接拉下 `optd` 源码动手调试，按状态依赖顺藤摸瓜只需读通八个核心文件：

1. [`optd-core/src/nodes.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-core/src/nodes.rs)：看清 `PlanNode`、`PredNode` 与 `PlanNodeOrGroup` 的分界；
2. [`optd-core/src/cascades/memo.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-core/src/cascades/memo.rs)：跟进 `add_new_group_expr_inner` 与 `merge_group_inner` 的去重和级联合并；
3. [`optd-core/src/cascades/rule_match.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-core/src/cascades/rule_match.rs)：看模式匹配器如何穿过子 `Group` 枚举笛卡尔积 `Binding`；
4. [`optd-datafusion-repr/src/rules/joins.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-datafusion-repr/src/rules/joins.rs)：对照 `JoinCommute`、`JoinAssoc` 与 `HashJoinRule` 体会列偏移与模式守恒；
5. [`optd-core/src/cascades/tasks2.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-core/src/cascades/tasks2.rs)：顺着 `optimize_group_inner` 追完交错计价与上下界传递；
6. [`optd-datafusion-repr/src/cost/base_cost.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-datafusion-repr/src/cost/base_cost.rs)：对照公式手算测试集里的 Winner；
7. [`optd-datafusion-repr/src/lib.rs`](https://github.com/cmu-db/optd/blob/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-datafusion-repr/src/lib.rs)：查看默认规则集、两阶段切换与 `step_clear_winner`；
8. [`optd-datafusion-bridge/src/`](https://github.com/cmu-db/optd/tree/e31c8e5779538916dca04fad253fdb4d2bdf5ea7/optd-datafusion-bridge/src)：串联 `into_optd.rs`、`from_optd.rs` 与 `physical_collector.rs`。

跑单测时记得打开 `dump_memo_table` 和 `enable_tracing`——因为最终吐出来的那棵 `EXPLAIN` 物理树，已经抹掉了搜索途中成百上千次交锋的痕迹；而一个优化器真正的智慧与隐疾，全写在那些“为什么没能当上 Winner”的被拒候选里。下一篇，我们就走进生产级的 [StarRocks Optimizer]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}})，看看当 `PhysicalPropertySet`、网络 `Exchange` 和多阶段聚合全面入场后，这张 Cascades 账本会演化成什么模样。

## 参考资料

- [cmu-db/optd 仓库源码](https://github.com/cmu-db/optd/tree/e31c8e5779538916dca04fad253fdb4d2bdf5ea7)
- Yongwen Xu, [*Efficiency in the Columbia Database Query Optimizer*](https://15721.courses.cs.cmu.edu/spring2019/papers/22-optimizer1/xu-columbia-thesis1998.pdf) (1998)
- Viktor Leis et al., [*How Good Are Query Optimizers, Really?*](https://15721.courses.cs.cmu.edu/spring2024/papers/16-costmodels/p204-leis.pdf), PVLDB 2015
- Timo Kersten et al., [*Adaptive Optimization of Very Large Join Queries*](https://arxiv.org/abs/1902.08291), SIGMOD 2018

---

> **源码基线说明**：本文源码路径、类型定义与测试 Trace 均基于 `cmu-db/optd` 提交 `e31c8e5`（2026-09-01，Workspace 锁定 DataFusion `55.0.0`）。注意仓库内部分早期设计文档仍描述统一的 `RelNode` 与显式任务栈，实际实现已演进为 `PlanNode` / `PredNode` 分离结构与 `tasks2.rs` 扩栈异步递归调用，阅读时请以本文引用的源码为准。

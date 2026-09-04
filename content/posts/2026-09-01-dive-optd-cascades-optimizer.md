---
title: "【源码】从零实现一个 Cascades Optimizer：optd 全链路源码剖析"
date: 2026-09-01T00:00:00+08:00
lastmod: 2026-09-01T00:00:00+08:00
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
description: "从亲手实现一个现代 Cascades Optimizer 的视角，结合 optd 当前源码，完整分析计划表示、Memo、规则匹配、任务搜索、代价与统计、Winner、两阶段优化、自适应反馈及 DataFusion 执行计划落地。"
draft: false
---

查询优化器最容易被误解为一组规则：把 Filter 推到 Scan 上，把 Join 交换一下，再从若干物理算子中选一个代价最低的计划。沿着这种视角阅读源码，最后往往记住很多类名，却仍然回答不了一个根本问题：**优化器究竟在维护什么状态，又如何证明自己没有错过更好的计划？**

真正的 Cascades Optimizer 不是“不断改写一棵树”，而是一台受预算约束的搜索引擎。它需要同时维护：

- 哪些表达式具有相同关系语义；
- 哪些规则还没有在某个表达式上触发；
- 某个物理要求下有哪些可执行实现；
- 子计划的最优解何时可以复用；
- 当前最优代价能否剪掉尚未展开的分支；
- 当统计信息变化时，哪些搜索结果可以保留，哪些必须重新计算。

本文选择 [optd](https://github.com/cmu-db/optd) 作为解剖对象。它的代码量远小于生产数据库优化器，却已经把计划表示、Memo、Transformation/Implementation Rule、Cost Model、属性推导、部分搜索、自适应反馈和 DataFusion Bridge 串成了一条可运行链路。更重要的是，它仍保留着一些没有被复杂工程包装掩盖的边界，适合回答两个互相补充的问题：

1. 如果从零实现一个现代 Cascades Optimizer，最小正确内核应该是什么？
2. optd 当前源码已经走到哪里，从教学原型走向生产系统还缺什么？

## 核心观点

先给出本文最重要的结论，后文的源码分析都围绕这些判断展开。

1. **优化器的核心对象不是 Plan Tree，而是带等价关系的搜索空间。** Plan Tree 只是输入和最终输出；Memo 中的 Group 与 MExpr 才是搜索期间的事实来源。
2. **Group 表示语义，MExpr 表示实现语义的一种结构。** Transformation Rule 向同一 Group 增加逻辑等价表达式，Implementation Rule 向同一 Group 增加物理实现；Winner 是在候选中做动态规划后的结果。
3. **规则正确性与代价最优性是两件事。** 规则负责保证新表达式语义等价，Cost Model 只在合法候选之间排序；错误的等价变换无法靠代价模型补救。
4. **统计信息不是 Cost 的附属字段，而是父子动态规划的中间状态。** 子节点先产生 Cardinality，父节点才能计算 Join、Sort、Aggregate 的局部代价与输出基数。
5. **现代 Cascades 的搜索子目标应是 `Optimize(Group, RequiredProperties, UpperBound)`。** optd 当前 Cascades 主链实际只使用 `Group + UpperBound`，每个 Group 也只有一个 Winner；required physical properties 尚未真正进入 Memo 搜索，这是它与生产级优化器之间最关键的边界。
6. **穷举不是 Cascades 的目标，可控地停止搜索才是工程能力。** optd 通过两阶段优化、空间预算、任务预算和 Branch-and-Bound，在“计划质量、规划延迟、一定能产出物理计划”之间做取舍。
7. **自适应优化不是另起一套优化器。** optd 保留 Memo 和已生成的候选，仅清空 Winner，再用执行期采集的 Group Cardinality 重新计价；这一设计揭示了 Memo 在 Re-optimization 中的真正价值。

### 源码分析基线

本文基于本地 `/Users/lism/xwork/optd` 源码逐项核对，分析快照如下：

| 项目 | 源码快照 | 日期 | 重点范围 |
|------|----------|------|----------|
| optd | [e31c8e5](https://github.com/cmu-db/optd/tree/e31c8e5779538916dca04fad253fdb4d2bdf5ea7) | 2026-09-01 | optd-core、DataFusion Repr/Bridge、基础与高级 Cost、自适应执行 |
| DataFusion | 55.0.0 | 由 optd Workspace 锁定 | LogicalPlan 接入、ExecutionPlan 落地 |

optd 的文档保留了早期实现的设计说明，但当前代码已经发生过重要演进。例如文档中的关系与标量表达式共用一棵 `RelNode`，当前源码则拆成 `PlanNode` 与 `PredNode`；文档描述经典显式 Task Stack，当前 `tasks2.rs` 使用相互递归的异步任务并通过扩栈运行。本文以源码快照为准，并在涉及设计意图时再参考文档。

## 一、先定义优化器真正要解的问题

### 从一棵树到一个搜索空间

考虑三个表的连接：

```sql
SELECT *
FROM a
JOIN b ON a.k = b.k
JOIN c ON b.k = c.k;
```

输入逻辑计划可能是一棵左深树：

```text
Join(Join(Scan(a), Scan(b)), Scan(c))
```

但逻辑等价结构至少还包括：

```text
Join(Scan(a), Join(Scan(b), Scan(c)))
Join(Scan(c), Join(Scan(b), Scan(a)))
...
```

每个逻辑 Join 又可能对应 Nested Loop Join、Hash Join，未来还可能有 Merge Join；每个子计划还可能要求不同的 Distribution、Ordering 或 Partitioning。直接复制 Plan Tree 会产生大量重复子树，并让“同一个子问题的最佳解”难以复用。

Cascades 的关键转变是：**不再问如何把当前树改得更好，而是问同一语义等价类中有哪些表达式，以及在一个物理要求下哪个表达式最便宜。**

理想化的搜索子目标可以写成：

```text
Optimize(group_id, required_properties, upper_bound) -> Winner
```

- `group_id`：要实现哪一个关系语义；
- `required_properties`：父算子要求输出具有什么物理性质；
- `upper_bound`：超过多少代价就不值得继续搜索；
- `Winner`：满足要求且成本最低的物理表达式及其子 Winner。

这三个输入分别对应 Cascades 的三根支柱：Memo 等价类、物理属性、基于代价的剪枝。

### 四个最小核心对象

如果亲手实现第一版优化器，我会先写出下面四个对象，而不是先堆规则：

```rust
struct Group {
    expressions: Vec<ExprId>,
    logical_properties: LogicalProperties,
    winners: HashMap<RequiredProperties, Winner>,
}

struct MExpr {
    operator: Operator,
    children: Vec<GroupId>,
    predicates: Vec<PredId>,
}

trait Rule {
    fn matches(&self, expr: ExprId, memo: &Memo) -> Vec<Binding>;
    fn apply(&self, binding: Binding) -> Vec<Expression>;
}

struct Winner {
    expr_id: ExprId,
    child_winners: Vec<WinnerKey>,
    cost: Cost,
    statistics: Statistics,
}
```

这不是 optd 类型定义的原样复制，而是生产级心智模型。对照当前 optd 源码可以看到：`Group`、`MemoPlanNode`、`Rule`、`WinnerInfo` 都已经存在，但 `winners` 目前仍简化成一个 `winner`，尚未以 `RequiredProperties` 为 Key。

## 二、optd 的整体架构：优化器内核与执行引擎解耦

optd Workspace 中与主链最相关的模块可以整理为：

```text
DataFusion SQL / LogicalPlan
          │
          ▼
optd-datafusion-bridge
  into_optd.rs       DataFusion LogicalPlan → optd PlanNode
          │
          ▼
optd-datafusion-repr
  plan_nodes.rs      DataFusion 领域的算子与谓词类型
  rules/             逻辑变换与物理实现规则
  properties/        Schema、ColumnRef 等逻辑属性
  cost/              基础代价与运行时自适应代价
          │
          ▼
optd-core
  nodes.rs           通用 Plan/Predicate 表示
  cascades/memo.rs   Group、MExpr、去重与 Group Merge
  cascades/tasks2.rs 搜索任务、规则触发、剪枝、Winner
  cost.rs            CostModel 抽象
          │
          ▼
optd-datafusion-bridge
  from_optd.rs       optd Physical Plan → DataFusion ExecutionPlan
  physical_collector 运行时 Cardinality 采集
          │
          ▼
DataFusion ExecutionPlan
```

这个分层很值得自研优化器借鉴：

- `optd-core` 不知道 Scan、Join 或 DataFusion 的具体枚举，只要求 `NodeType` 能区分逻辑与物理算子；
- `optd-datafusion-repr` 定义领域模型、规则、属性和代价；
- `optd-datafusion-bridge` 负责边界翻译，不把 DataFusion 类型泄漏进 Memo；
- Advanced Cost 作为独立 crate 接入，说明统计估算可以替换，而不必改搜索内核。

所以“通用优化器框架”并不等于自己实现 SQL Parser、Catalog 和执行器。一个更现实的学习路径是：**复用 DataFusion 生成合法 LogicalPlan，自己实现从关系代数到物理计划之间的完整搜索。**

## 三、全链路入口：SQL 如何进入并离开 optd

### 接管 DataFusion 的 QueryPlanner

入口位于 `optd-datafusion-bridge/src/lib.rs`。`create_df_context` 做了三件决定权归属的工作：

```rust
if !use_df_logical {
    session_config.options_mut().optimizer.max_passes = 0;
    builder = builder.with_optimizer_rules(vec![]);
}

builder = builder.with_physical_optimizer_rules(vec![]);
builder = builder.with_query_planner(optimizer.clone());
```

当 `use_df_logical=false` 时，DataFusion 自带的逻辑规则被关闭；DataFusion Physical Optimizer Rule 无论如何都会被清空；最终由 `OptdQueryPlanner` 接管物理规划。这样才能保证 Explain 中看到的 Join Reorder、物理算子选择与 Cost 都来自 optd，而不是两套优化器混合后的结果。

`OptdQueryPlanner::create_physical_plan_inner` 的主流程可以压缩为：

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

一条查询实际经历：

```text
DataFusion LogicalPlan
  → optd Logical Plan
  → Heuristic 规范化/子查询解关联
  → Cascades Memo 搜索
  → 提取 optd Physical Plan
  → DataFusion ExecutionPlan
```

DML、DDL 等 optd 尚未支持的顶层计划会回退到 `DefaultPhysicalPlanner`。这是接入新执行引擎时应保留的工程策略：先明确支持矩阵，让未覆盖语义走可靠的 Fallback，而不是在搜索深处以 `unimplemented!` 暴露给用户。

### 为什么先有一轮 Heuristic Optimizer

`DatafusionOptimizer::default_heuristic_rules` 注册的主要是：

- 无效 Project、Filter、Join、Limit 消除；
- Predicate 简化与 Project/Filter Merge；
- 重复 Sort/Aggregate 表达式消除；
- Dependent Join 消除与下推。

这说明 RBO 与 CBO 并不是二选一。确定性规范化放在 Memo 之前有三个好处：

1. 消除不会带来有价值备选的语法噪声；
2. 把相关子查询先转换为普通关系算子，降低 Cascades Rule 的状态空间；
3. 避免等价但形状不同的表达式过早污染 Memo。

但这条原则不能无限外推。像 Filter Pushdown、Join Reorder 这种变换可能改变中间结果基数，并与物理算子组合产生不同总代价，应该保留在 Memo 中比较，而不是在 Heuristic 阶段不可逆地覆盖原计划。

### DataFusion LogicalPlan 到 optd PlanNode

`optd-datafusion-bridge/src/into_optd.rs` 当前覆盖：

| DataFusion 节点 | optd 节点 | 关键处理 |
|-----------------|-----------|----------|
| TableScan | LogicalScan | 保存 TableSource，Projection 转成显式 Project |
| Projection | LogicalProjection | 表达式转 PredNode，子查询转 RawDependentJoin |
| Filter | LogicalFilter | 列名解析为位置索引，收集子查询 |
| Join | LogicalJoin | 右侧列索引增加左 Schema 宽度 |
| Aggregate | LogicalAgg | 分离 Aggregate Expr 与 Group Expr |
| Sort/Limit | LogicalSort/LogicalLimit | 排序方向、skip/fetch 转谓词 |
| SubqueryAlias | 透明穿过 | optd 内部主要使用位置列引用 |
| EmptyRelation | LogicalEmptyRelation | 编码输出 Schema 与是否生成一行 |

这一层最重要的设计不是类型转换，而是**建立优化器内部稳定的列引用语义**。optd 使用位置索引：Join 条件中 `[0, left_width)` 属于左孩子，右侧从 `left_width` 开始。Join Commute 与 Join Assoc 改变孩子顺序时，必须同步重写列索引，否则结构上“等价”，语义上已经错误。

这也解释了为什么规则代码不能只移动 PlanNode：关系代数结构、谓词引用范围与输出 Schema 必须一起保持不变量。

## 四、计划表示：为什么 Plan 与 Predicate 要分开

当前 `optd-core/src/nodes.rs` 的核心表示是：

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

它把两类边分开：

```text
PlanNode.children    关系输入，可以是完整子树，也可以是 Memo Group 占位符
PlanNode.predicates  算子参数，始终是完整的标量表达式树
```

在 DataFusion 领域中，`DfNodeType` 包含 Logical/Physical Scan、Projection、Filter、Join、Sort、Agg、Limit 等关系算子；`DfPredType` 包含 ColumnRef、Constant、BinOp、LogOp、Func、Cast、Like、InList、SortOrder 等标量节点。

这种拆分比“所有内容都是 RelNode”更利于保持边界：

- Memo 的关系等价类只围绕 Plan Child 建立；
- Predicate 可以单独 Intern，多个 MExpr 共享同一条件；
- Rule Matcher 可以明确匹配算子形状，而不是误把表达式子树当关系输入；
- Schema 和 Cardinality 沿关系边传播，表达式类型与引用分析沿谓词树传播。

当前限制也同样明确：PredNode 会被 Intern，但不会像关系算子一样进入 Cascades 等价搜索。因此常量折叠、布尔规范化、表达式公共子表达式、不同函数实现选择，主要依赖 Heuristic Rule 或执行引擎，而不是统一的 Memo 搜索。

## 五、Memo：Cascades 的真正内核

### Group、MExpr 与 Pred

`optd-core/src/cascades/memo.rs` 中，进入 Memo 的关系表达式不再持有子树：

```rust
pub struct MemoPlanNode<T: NodeType> {
    pub typ: T,
    pub children: Vec<GroupId>,
    pub predicates: Vec<PredId>,
}
```

这就是 Cascades 论文中的 MExpr。三个标识符分别表示：

| 标识符 | 含义 | 是否表示语义等价类 |
|--------|------|--------------------|
| `GroupId` | 一组输出语义等价的表达式 | 是 |
| `ExprId` | 某个具体 MExpr | 否 |
| `PredId` | Intern 后的谓词表达式 | 否 |

例如同一个 Group 可能包含：

```text
Group !31
  expr 8  : Filter(!6, #0 = #2)
  expr 15 : Join(Inner, !2, !2, #0 = #2)
  expr 25 : PhysicalNestedLoopJoin(!2, !2, #0 = #2)
  expr 28 : PhysicalHashJoin(!2, !2, left=[#0], right=[#0])
```

这里 `Filter(CrossJoin)` 与 `InnerJoin` 来自逻辑变换，NLJ 与 HashJoin 来自实现规则。它们共享同一输出 Schema 和关系语义，所以进入同一 Group；但只有物理表达式有资格成为最终 Winner。

### 插入：先递归 Memoize 孩子，再做结构去重

`NaiveMemo::add_new_group_expr_inner` 的过程是：

```text
完整 PlanNode
  → 递归把每个 Plan Child 插入 Memo，得到 children_group_ids
  → 把 Predicate Intern 成 pred_ids
  → 构造 MemoPlanNode(operator, child groups, pred ids)
  → 查询 expr_node_to_expr_id 做结构去重
  → 新表达式：创建 ExprId，并加入目标 Group 或新 Group
  → 已存在表达式：复用 ExprId，必要时触发 Group Merge
```

结构去重的 Key 是整个 `MemoPlanNode`。因此只要 Operator、Child Group 和 Predicate 都相同，它就是同一个 MExpr，不会因为由不同规则生成而重复占用搜索空间。

### 规则结果为什么必须加入原 Group

Transformation Rule 声明的是语义等价关系。若规则把：

```text
(A Join B) Join C
```

转换为：

```text
A Join (B Join C)
```

新根表达式必须插回旧根的 Group。`tasks2.rs` 在应用规则后调用：

```rust
self.optimizer.add_expr_to_group(expr, group_id)
```

这行代码是规则契约的落点：**Rule 的返回值不是一棵用来覆盖旧树的新树，而是“同一语义的又一种表达方式”。** 如果 Rule 实际上不等价，整个 Group 的逻辑属性与 Winner 都会失去意义。

### Group Merge：等价关系会向父节点传播

如果新表达式已经存在于另一个 Group，两个 Group 必须合并。当前 `NaiveMemo::merge_group_inner` 会：

1. 把被合并 Group 的全部 Expr 移到目标 Group；
2. 合并已有 Winner，保留代价更低者；
3. 更新旧 Group 到新 Group 的重定向映射；
4. 遍历所有父 MExpr，把 Child GroupId 改为合并后的 ID；
5. 如果父 MExpr 因此结构重复，继续合并父 Group。

这说明等价类不是局部容器，而是一张带父子引用的图。孩子等价会导致父表达式相同，父 Group 也可能发生级联合并。

当前实现故意命名为 `NaiveMemo`：Group 重定向仍使用映射并全量更新，源码也留下使用 Union-Find 等结构优化的注释。生产系统还需要 Parent Index、并发安全、稳定 ID、增量失效和更高效的 Duplicate Detection，但这些都是在相同语义模型上的工程增强。

### 逻辑属性属于 Group，而不是 MExpr

`Group` 创建时通过第一个 MExpr 推导逻辑属性：

```rust
pub struct Group {
    group_exprs: HashSet<ExprId>,
    info: GroupInfo,
    properties: Arc<[Box<dyn LogicalProperty>]>,
}
```

当前 DataFusion Repr 注册两个 Logical Property Builder：

- `SchemaPropertyBuilder`：输出列名、类型、Nullable；
- `ColumnRefPropertyBuilder`：输出列追溯到哪个 Base Table Column，并维护等值列相关性。

逻辑属性只在 Group 初始化时计算一次，后续等价表达式共享。这其实提供了一个很强的 Rule 校验思路：如果一条新表达式加入 Group 后推导出的 Schema、Column Lineage 或其他逻辑属性与 Group 不一致，说明规则很可能破坏了语义等价。当前实现相信 Rule 的正确性，没有对每次插入重新验证；自研版本可以在 Debug 模式增加这一断言。

## 六、规则系统：结构匹配只是开始，语义维护才是难点

### Transformation Rule 与 Implementation Rule

optd 的 `Rule` 抽象非常小：

```rust
pub trait Rule<T, O> {
    fn matcher(&self) -> &RuleMatcher<T>;
    fn apply(&self, optimizer: &O, binding: ArcPlanNode<T>)
        -> Vec<PlanNodeOrGroup<T>>;
    fn name(&self) -> &'static str;
    fn is_impl_rule(&self) -> bool { false }
}
```

两类规则的职责必须区分：

| 规则 | 输入与输出 | 作用 |
|------|------------|------|
| Transformation | Logical → Logical | 扩展等价逻辑空间，如 Join Commute、Join Assoc、Filter Pushdown |
| Implementation | Logical → Physical | 为逻辑语义提供可执行实现，如 Join → NLJ/HashJoin |

探索子 Group 时只触发 Transformation Rule，因为此时目标是补齐上层 Rule 可能匹配的逻辑 Binding；真正优化 Group 时才触发 Implementation Rule。这个差异避免了为了匹配一个逻辑规则，提前把整个子树的物理实现全部展开。

### Matcher 如何从 Memo 中还原 Binding

当前 `RuleMatcher` 的主要形态是：

```text
MatchNode / MatchDiscriminant
Any
AnyMany
```

以 Join Associativity 为例：

```rust
define_rule!(
    JoinAssocRule,
    apply_join_assoc,
    (Join(JoinType::Inner), (Join(JoinType::Inner), a, b), c)
);
```

Matcher 先锁定根 MExpr，再进入左 Child Group，枚举该 Group 内所有能匹配 Inner Join 的 MExpr。对于嵌套孩子，它递归匹配；对于 `Any`，只保留 Group 占位符。多个孩子的匹配结果最后做笛卡尔积，形成完整 Binding。

这套实现直观，但也揭示了规则爆炸的来源：

```text
父规则 Binding 数
  = Child Group 1 的匹配数
  × Child Group 2 的匹配数
  × ...
```

optd 在单次规则得到至少 200 个 Binding 时记录警告，但当前仍会先物化全部 Binding。生产实现通常需要 Lazy Iterator、Promise/Rule Priority、Binding 上限，以及更细粒度的 Pattern Index。

### Join Commute：移动孩子只是最简单的一半

`rules/joins.rs` 中的交换规则不是简单返回 `Join(right, left)`。它还要：

1. 获取左右输入 Schema 宽度；
2. 重写 Join Condition 中的列索引；
3. 在交换后的 Join 上增加 Projection；
4. 用 Projection 恢复原始输出列顺序。

可以抽象为：

```text
A columns: [0, a_width)
B columns: [a_width, a_width + b_width)

交换后条件索引：
  A.col(i) → b_width + i
  B.col(j) → j - a_width

交换后输出：[B..., A...]
恢复输出：  Project[A..., B...]
```

Projection 不是多余节点，而是让新表达式继续满足原 Group 输出 Schema 的必要条件。后续 `ProjectionPullUpJoin`、`ProjectMergeRule` 等规则再负责消除或移动它。

### Join Assoc：只有可安全迁移的谓词才能结合

当前结合律实现：

```text
(A Join B on cond1) Join C on cond2
  →
A Join (B Join C on rewrite(cond2)) on cond1
```

它尝试把 `cond2` 的列引用去掉 A 的宽度。如果 `cond2` 引用了 A，`rewrite_column_refs` 返回 `None`，规则直接放弃。也就是说，规则不是看到形状就生效，还要证明谓词只依赖 B、C，能够安全迁移到新内层 Join。

当前规则仅处理 Inner Join，恰好说明一个普遍原则：Outer Join、Semi/Anti Join 的交换结合受 Null-Generating Side、谓词来源与输出列语义限制，不能把 Inner Join 代数定律直接复用。

### HashJoinRule：物理实现规则也需要能力检查

`HashJoinRule` 当前只接受：

- Inner Join；
- 一个等值条件，或连续 `AND` 连接的等值条件；
- 等值两侧最终都是 ColumnRef；
- 一侧列来自左 Schema，另一侧来自右 Schema。

它把全局 Join 列索引改写成左右孩子各自的局部索引，再生成：

```text
PhysicalHashJoin(
  left_group,
  right_group,
  left_keys=[...],
  right_keys=[...]
)
```

不满足条件时返回空结果，逻辑 Join 仍可由通用 Physical Conversion Rule 转为 Nested Loop Join。这保证了“Hash Join 不适用”不会等于“查询无法执行”。

但当前能力边界也很清晰：非 Inner Hash Join、表达式 Join Key、Residual Join Filter、Build/Probe 方向选择、Broadcast/Shuffle 模式都没有进入该规则的候选空间。

## 七、搜索任务：Cascades 如何把规则与动态规划串起来

### 当前实现不是文档中的显式 Task Stack

经典 Cascades 常用显式任务栈描述：

```text
OptimizeGroup
  → OptimizeExpression
    → ExploreGroup
    → ApplyRule
    → OptimizeInputs
```

当前源码 `optd-core/src/cascades/tasks2.rs` 保留了相同任务语义，但使用相互递归的 `async fn`。`fire_optimize_tasks` 通过 `stacker::grow` 提供 32 MB 栈，再用 `pollster` 同步运行 Future。

这对理解算法很友好，因为源码调用关系与伪代码接近；对生产实现则意味着后续应考虑显式 Worklist，以降低深递归的栈占用，并支持任务优先级、暂停恢复、并行搜索与更完整的 Trace。

### OptimizeGroup：先评价已有物理表达式，再展开逻辑表达式

`optimize_group_inner` 的顺序是：

```text
if group already explored: return
mark group explored

for physical expr in group:
    OptimizeInput(expr)

for logical expr in group:
    OptimizeExpr(expr, exploring=false)
```

先评价物理表达式可以尽早得到一个 Winner Upper Bound，后续新物理候选就能用它剪枝。随后处理逻辑表达式，规则产生的新物理 MExpr 会立刻进入 `OptimizeInput`，产生更好的 Winner 后又会收紧 Upper Bound。

### OptimizeExpr：Rule Fired Set 是搜索状态的一部分

每个 `(ExprId, RuleId)` 只触发一次。优化器维护：

```rust
fired_rules: HashMap<ExprId, HashSet<RuleId>>
```

`OptimizeExpr` 先检查根 Operator 是否可能匹配 Rule，再探索 Child Group，最后调用 `ApplyRule`。探索模式下跳过 Implementation Rule，预算耗尽后跳过 Transformation Rule。

Rule Fired Set 很重要：没有它，Join Commute 可能在 `A Join B` 与 `B Join A` 之间无限往复；仅依赖 Memo 去重仍会反复做匹配和 Apply。Fired Set 把“这个表达式上的这条推导已经尝试过”显式记录下来。

### ApplyRule：新表达式被递归纳入同一次搜索

`ApplyRule` 的关键步骤是：

```text
检查 disabled / fired
  → 标记 fired
  → 从 Memo 枚举 Binding
  → 执行 Rule.apply
  → 把结果加入原 Group
  → 新 Logical Expr：OptimizeExpr
  → 新 Physical Expr：OptimizeInput
```

因此 Cascades 不是“先生成完全部逻辑计划，再统一物理化”。逻辑扩展与物理评价交错发生，新的低成本 Winner 可以尽早反过来剪掉后续搜索。

### OptimizeInput：自底向上的 Cost 与自顶向下的 Upper Bound

`OptimizeInput` 是代价搜索的核心。对于一个物理 MExpr，它依次优化每个 Child Group：

```text
parent upper bound
  ├── 已知 operator cost
  ├── 已决定 child cost
  └── 当前处理 child 的可用预算
```

源码中的子节点上界近似为：

```text
child_upper_bound
  = parent_upper_bound
  - cost_so_far
  + current_child_cost
```

如果 `cost_so_far > upper_bound`，当前物理表达式直接被剪掉。所有孩子都有 Winner 后，再完整计算：

```text
operation_cost = Cost(operator, child_statistics)
total_cost     = operation_cost + sum(child_total_cost)
statistics     = DeriveStatistics(operator, child_statistics)
```

若 `weighted(total_cost)` 更低，就覆盖 Group Winner。

这里同时存在两种信息流：

```text
Upper Bound：父 → 子，用于限制搜索
Cost/Statistics：子 → 父，用于动态规划
```

这正是 Cascades 与简单 Bottom-Up DP 的区别：它不仅复用子问题最优解，还通过父问题的当前最好解主动限制子问题探索。

当前剪枝仍是保守的简化版本。源码 TODO 明确指出，尚未把“未决定孩子的 Lower Bound”加入 `cost_so_far`；它们暂时按零计算。因此当前实现可能少剪枝，但不会仅因为这一点错误剪掉更优计划。

## 八、Cost、Statistics 与 Winner：最优计划如何被选出

### CostModel 的职责边界

`optd-core/src/cost.rs` 把代价模型抽象为：

```rust
fn compute_operation_cost(
    node,
    predicates,
    children: &[Option<&Statistics>],
    context,
    optimizer,
) -> Cost;

fn derive_statistics(
    node,
    predicates,
    children: &[&Statistics],
    context,
    optimizer,
) -> Statistics;
```

此外还提供 `zero`、`accumulate`、`sum`、`weighted_cost` 和 Explain。这里刻意把两件事分开：

- Statistics 描述数据规模与分布，是父节点估算的输入；
- Cost 描述执行工作量，用于候选排序。

同样的 Cardinality 可以映射为不同硬件下的 CPU、I/O、Network、Memory 或 Latency Cost；同样的 Operator Cost 也可能因为并发度和资源约束产生不同最终排序。

### 基础 Cost Model 的公式

`optd-datafusion-repr/src/cost/base_cost.rs` 使用二维 Cost：

```text
Cost = [compute_cost, io_cost]
weighted_cost = compute_cost + io_cost
```

主要公式可以整理为：

| 物理算子 | 输出行数估算 | 局部代价 |
|----------|--------------|----------|
| Scan | 表统计，缺省 1000 | `io = rows` |
| Filter | `child × 0.01`，至少 1 | `child_rows × predicate_cost` |
| NLJ | `left × right × 0.01` | `left × right × predicate_cost + left` |
| HashJoin | `min(left, right)` | `2 × left + right` |
| Projection | 与孩子相同 | `child_rows × expression_cost` |
| Sort | 与孩子相同 | `rows × ln(1 + rows)` |
| Aggregate | 与孩子相同 | `rows × (agg_expr_cost + group_expr_cost)` |

这套公式显然不是生产模型。例如 Filter Selectivity 固定为 1%，基础 Limit 的 Cardinality 没有使用 fetch，Hash Join 的输出行数取两侧最小值。但它已经足以验证搜索内核：当 Join Order 改变中间基数时，总代价会变化；Hash Join 与 NLJ 也会因为复杂度差异产生稳定排序。

一个好的学习顺序正是如此：**先让 Cost Model 简单到可以手算，验证 Memo 与 Winner；再逐步增加统计精度，而不是从一开始就把 Histogram、NDV 和相关性全部塞进系统。**

### Winner 记录的不是一个 ExprId

当前 `WinnerInfo` 保存：

```rust
pub struct WinnerInfo {
    pub expr_id: ExprId,
    pub total_weighted_cost: f64,
    pub operation_weighted_cost: f64,
    pub total_cost: Cost,
    pub operation_cost: Cost,
    pub statistics: Arc<Statistics>,
}
```

它同时保留局部代价、累计代价和输出统计，便于：

- 父节点复用子节点累计 Cost；
- Explain 区分某个算子自身贵，还是孩子贵；
- 父节点基于子 Statistics 估算自己；
- 自适应重优化时清空 Winner 后重新计算。

不过当前 `GroupInfo` 只有一个 `winner: Winner`。如果一个 Group 同时需要：

```text
Winner[Ordering = (a)]
Winner[Distribution = Hash(a)]
Winner[Distribution = Single]
```

单一 Winner 无法表达这些互不可替代的最优解。这也是后文生产化路线中第一优先级要修改的数据结构。

## 九、用一条真实 Memo Trace 手算 Winner

optd 自带的 `optd-sqlplannertest/tests/utils/memo_dump.planner.sql` 使用自连接：

```sql
SELECT *
FROM t1 AS a, t1 AS b
WHERE a.t1v1 = b.t1v1
ORDER BY a.t1v1;
```

最终 optd Physical Plan 是：

```text
PhysicalSort
├── exprs: SortOrder(Asc, #0)
└── PhysicalHashJoin(Inner)
    ├── left_keys:  [#0]
    ├── right_keys: [#0]
    ├── PhysicalScan(t1)
    └── PhysicalScan(t1)
```

Memo Trace 中，两个别名 Scan 共享相同表语义并复用 `Group !2`。连接语义最后收敛到 `Group !31`，其中同时包含 Filter + Cross Join、Logical Join、Physical NLJ、Physical Hash Join，以及 Join Commute 产生的 Projection 组合。

基础模型缺省认为每次 Scan 1000 行：

```text
Scan(t1) operation cost = {compute=0, io=1000}
两个 Scan total         = {compute=0, io=2000}
```

Hash Join 的局部代价为：

```text
2 × left_rows + right_rows
= 2 × 1000 + 1000
= 3000 compute
```

所以连接 Group 的 Hash Join Winner：

```text
total = children 2000 IO + operator 3000 compute
      = weighted_cost 5000
```

NLJ 则需要约 `1000 × 1000` 级别计算，Trace 中总加权代价为 `1,003,000`，因此很快被 Hash Join 取代。

Hash Join 输出仍估为 1000 行，Sort 局部代价：

```text
1000 × ln(1001) ≈ 6908.75
```

最终根 Group Winner：

```text
5000 + 6908.75 = 11908.75
```

这个例子非常适合验证自己写的第一版优化器：Memo 中要同时看见逻辑与物理候选，Winner 替换过程必须可解释，手算总代价还要与 Trace 一致。如果这三个条件做不到，继续增加规则只会让错误更难定位。

## 十、两阶段优化与预算：计划质量必须服从规划延迟

### 为什么先禁用 Join Reorder

`DatafusionOptimizer::cascades_optimize` 把一次优化拆成两阶段：

```text
Stage 1
  disable JoinCommute / JoinAssoc
  插入计划并完成第一次搜索
  得到不改变 Join Order 的可执行 Winner

Stage 2
  enable JoinCommute / JoinAssoc
  清空 explored 状态
  在已有 Memo 与 Winner 上继续搜索
```

Stage 1 的价值不是产出最终最优计划，而是尽快得到一个合法物理计划和 Upper Bound。Stage 2 扩展最容易爆炸的 Join Order 空间时，已有 Winner 可以用于剪枝；即使预算耗尽，系统也不会因为只生成了逻辑表达式而没有可执行结果。

需要注意的是，`step_next_stage` 只清空 `explored_group` 与 `explored_expr`，不会清空 Memo、Winner 或 Fired Rules。Stage 1 被禁用的 Join Rule 不会被标记为 fired，因此 Stage 2 可以正常触发；已经完成的其他推导则无需重复。

### 两类预算对应两种降级策略

默认参数是：

```rust
partial_explore_iter  = Some(1 << 18)
partial_explore_space = Some(1 << 14)
disable_pruning       = false
```

当 Memo 表达式数量超过空间预算时：

```text
停止 Transformation Rule
继续 Implementation Rule
```

当任务步数超过总预算时：

```text
停止继续扩展规则
如果 Group 已有完整 Winner，尽快结束
```

这种降级比简单抛出 Timeout 更有工程意义：逻辑搜索空间可以不完整，但已有逻辑表达式仍应被物理化，最终尽量返回一个可执行计划。

不过预算也会改变“最优”的含义。严格说，结果只是**已探索空间中的最低代价计划**，而不是完整规则闭包中的全局最优。生产 Explain 和 Metrics 应记录是否触发预算、哪些规则被截断、最终 Memo Space 多大，否则用户会把“搜索提前停止”误判为 Cost Model 选择错误。

### 当前搜索状态还不是完整的 Subgoal Cache

从生产级 Cascades 的角度，当前任务去重还有一个比性能更重要的边界：

```text
explored_group key = GroupId
TaskDesc key       = (ExprId, GroupId)
```

它们都没有包含 `UpperBound`，更没有 Required Physical Property。这意味着“在一个较紧上界下已经访问过”和“这个 Group 已被完整优化”会共享同一个 explored 标记。若某个子问题第一次在严格 Upper Bound 下没有找到 Winner，之后从更宽松的上下文再次进入时，理论上不应直接复用“已探索”结论。

更稳健的实现有两种方向：

1. 把 Required Property 与搜索完成状态纳入 Subgoal Key，并区分 `InProgress`、`Pruned(bound)`、`Complete(winner)`；
2. 仅缓存与 Upper Bound 无关的完整搜索结果，因 Bound 提前返回的任务不标记为全局 Complete。

另外，当前 `step_clear` 会重建 Memo 并清理 Fired/Explored Set，但没有重置 `OptimizerContext` 中的预算耗尽标记，也没有清空累计 `CascadesStats`。如果一个长生命周期 Optimizer 实例处理多条互不相关查询，一次查询触发预算后，后续查询可能继承降级状态。教学与基准场景可以观察累计指标，生产接入则应该明确拆分 Query-local Search State 与跨查询 Runtime Feedback State。

`Winner::Impossible` 虽然已经定义，当前任务主链在找不到孩子 Winner 时主要以 `Unknown` 提前返回，并没有形成完整的“不可能子目标”缓存。这些细节不会改变 Cascades 的核心模型，却决定了搜索结果能否在不同上下文中被安全复用。

## 十一、逻辑属性与物理属性：当前实现最重要的分界线

### Schema 与 Column Lineage 已进入 Group

逻辑属性必须在所有等价 MExpr 间保持一致。optd 当前的两个属性已经支持不少关键 Rule：

```text
Schema
  → Join 后列索引边界
  → Projection 输出宽度
  → Empty Relation 的输出语义

ColumnRef / SemanticCorrelation
  → 输出列追溯到 base_table.column
  → 等值列集合
  → Advanced Cost 中 Filter/Join 的列统计选择
```

`ColumnRefPropertyBuilder` 还使用并查集维护等值列相关性。例如 `a.k = b.k AND b.k = c.k` 可以形成一个等价列集合，为 Join Cardinality 和谓词推导提供语义事实。

这揭示了 Logical Property 的本质：它不是为了美化 Explain，而是让 Rule 和 Cost Model 可以查询“这个 Group 代表什么”，同时不依赖某一棵具体 Plan Tree。

### Physical Property 抽象存在，但尚未接入 Cascades 搜索

`optd-core/src/physical_property.rs` 已经定义：

- `derive`：从孩子物理属性推导当前输出属性；
- `passthrough`：把父节点要求传给孩子；
- `satisfies`：已有属性能否满足 Required Property；
- `enforce`：不满足时插入 Sort、Exchange 等 Enforcer；
- `default`：无特殊要求。

Heuristic Optimizer 已能调用这套抽象。但当前 Cascades 实现中的：

```rust
fn optimize_with_required_props(...) {
    unimplemented!()
}
```

而 DataFusion Cascades Optimizer 的构造也没有注册 Physical Property Builder。因此当前主链中的搜索子目标实际是：

```text
Optimize(GroupId, UpperBound)
```

而不是完整的：

```text
Optimize(GroupId, RequiredProperties, UpperBound)
```

这会带来直接限制：

- 一个 Group 只能保留一个 Winner；
- Sort Order 不能作为父子 Contract 参与候选复用；
- Hash Distribution、Broadcast、Single Partition 无法区分；
- Enforcer 不能作为带代价的候选进入统一搜索；
- Hash Join 的 Build/Probe 与 Exchange 组合无法一起比较。

如果要把 optd 推向分布式生产系统，最优先的改造不是再增加几十条 Rule，而是让 Winner Key、Task Key、Fired/Explored State 与 Costing 全部带上 Required Property。

一个最小演进版本可以是：

```rust
#[derive(Hash, Eq, PartialEq)]
struct WinnerKey {
    group_id: GroupId,
    required: PhysicalPropertySet,
}

struct GroupInfo {
    winners: HashMap<PhysicalPropertySet, Winner>,
}

enum TaskDesc {
    OptimizeGroup(WinnerKey, UpperBound),
    OptimizeExpr(ExprId, WinnerKey),
    OptimizeInputs(ExprId, WinnerKey),
}
```

同时，`OptimizeInput` 需要先把父 Required Property 通过 Operator Passthrough 到每个孩子，优化孩子后推导实际输出属性，不满足时生成 Enforcer，并把 Enforcer Cost 计入候选。

## 十二、自适应优化：复用搜索空间，只重算 Winner

### 执行期统计如何回到 Group

当 Adaptive 开启时，`from_optd.rs` 会在每个选中的物理节点外包一层 `CollectorExec`。计划提取阶段通过 `PlanNodeMetaMap` 把 materialized PlanNode 指针映射回 GroupId，于是 Collector 能记录：

```text
GroupId → (actual_row_count, iteration)
```

`CollectorReader` 在消费 RecordBatch 时累计行数，在 Stream 完成时写入共享 `RuntimeAdaptionStorage`。下一次规划时，`AdaptiveCostModel` 在 PhysicalScan 的 `RelNodeContext.group_id` 上查询最近的运行时行数；超过 decay 窗口后回退到缺省 1000 行。

链路可以画成：

```text
Memo Group !31
   │  提取 Winner，写入 PlanNodeMeta
   ▼
PhysicalHashJoin
   │  包装 CollectorExec(group=!31)
   ▼
实际执行行数
   │  RuntimeAdaptionStorage[!31] = rows
   ▼
下一轮清空 Winner，重新 Cost
```

### 为什么清空 Winner，而不是清空 Memo

非自适应模式调用 `step_clear`：清空 Memo、Fired Rules、Explored State，从输入计划重新搜索。

自适应模式调用 `step_clear_winner`：

- 保留 Memo 中已经生成的逻辑与物理候选；
- 保留 Fired Rules，避免重复扩展相同等价空间；
- 清空所有 Winner；
- 清空 Explored State；
- 用新 Cardinality 重新运行 Cost DP。

这是一种非常干净的 Re-optimization 模型：**结构搜索结果与代价评价结果分离。** SQL 语义和规则没有变化时，等价表达式空间可以复用；运行时只改变 Cardinality，因此重算 Winner 即可。

### 当前 Adaptive 闭环的限制

这套原型展示了方向，但还不是通用 Adaptive Query Execution：

1. `AdaptiveCostModel` 当前只在 PhysicalScan 直接读取 Group 运行时行数，其他算子仍按基础公式推导；
2. `CollectorExec` 只接受 `partition == 0`，不支持多分区结果汇总；
3. 反馈以 GroupId 为 Key，跨 SQL、跨 Memo 的稳定签名与持久化尚未解决；
4. 当前共享 Optimizer 通过 `Mutex<Option<Box<_>>>` 临时取出，面向并发查询需要更稳健的 Session/Query 生命周期；
5. Re-optimization 发生在下一次规划，不是单个长查询执行到中途后的 Plan Fragment 替换；
6. 旧 Memo 保留哪些候选受此前预算影响，若第一次搜索未生成某类 Join Order，重算 Winner 也无法凭空得到它。

因此更准确的定位是：optd 实现了一个基于 Memo 复用的 Runtime Cardinality Feedback 原型，而不是完整的 Mid-query Re-optimization。

## 十三、从 optd Physical Plan 落到 DataFusion ExecutionPlan

`optd-datafusion-bridge/src/from_optd.rs` 递归把已经完全 materialize 的物理计划转换为 DataFusion 算子：

| optd 物理节点 | DataFusion ExecutionPlan |
|---------------|--------------------------|
| PhysicalScan | TableProvider `scan` |
| PhysicalProjection | `ProjectionExec` |
| PhysicalFilter | `FilterExec` |
| PhysicalLimit | `GlobalLimitExec` |
| PhysicalSort | `SortExec` |
| PhysicalAgg | `AggregateExec(Single)` |
| PhysicalNestedLoopJoin | `NestedLoopJoinExec` / `CrossJoinExec` |
| PhysicalHashJoin | `HashJoinExec` |
| PhysicalEmptyRelation | `EmptyExec` / `PlaceholderRowExec` |

这里可以看到“选中物理算子”与“构造可执行算子”仍是两层：Cascades 只输出领域物理计划，Bridge 再解析 PredNode，依据输入 Schema 生成 DataFusion `PhysicalExpr`。

Hash Join 当前只支持 Inner Join，Key 必须是 ColumnRef，Residual Filter 为 `None`，并固定使用：

```rust
PartitionMode::CollectLeft
```

这与基础 Cost 中 `2 × left + right` 的方向假设相呼应，但也说明 Distribution 尚未成为真正候选。如果未来要比较 Broadcast Left、Broadcast Right、Partitioned Hash Join，就必须同时扩展：

```text
Physical Operator Variant
  + Required/Derived Distribution
  + Exchange Enforcer
  + Network/Memory Cost
  + DataFusion ExecutionPlan 参数
```

只在 Bridge 中切换 `PartitionMode`，而不让 Memo 和 Cost 看见这个选择，会让执行策略脱离 CBO。

## 十四、如果亲手实现：一条可以逐步验证的路线

理解优化器最有效的方法，是让每一步都能运行、能 Explain、能被手算验证。下面是一条从零到现代 Cascades 的实现顺序。

### 第 1 步：只支持三种逻辑算子

先定义：

```text
Scan(table)
Filter(input, predicate)
Join(left, right, condition)
```

标量表达式只支持 ColumnRef、Constant、Eq、And。此时不要做 SQL Parser，可以直接构造 Plan，或者接入 DataFusion/Calcite 的 LogicalPlan。

验收标准：Plan 与 Predicate 可以稳定 Hash、Equal、Display；Join 交换后列索引有单元测试。

### 第 2 步：实现 Memo Intern 与 Group

实现：

```text
PlanNode → MemoPlanNode(operator, child_groups, pred_ids)
```

加入三个不变量测试：

1. 相同 Scan 只产生一个 MExpr；
2. 相同结构重复插入返回同一 ExprId；
3. 等价孩子 Group 合并后，重复父表达式也会合并。

验收标准：Memo Dump 能稳定输出 Group、Expr、Pred。

### 第 3 步：只写一条 Transformation Rule

实现 Inner Join Commute，但必须连同：

- Condition Column Ref Rewrite；
- 输出 Projection Restore；
- Schema Property 校验。

验收标准：新表达式进入原 Group，规则重复运行不会无限增长。

### 第 4 步：加入 Implementation Rule 与最小 Cost

为 Join 提供 NLJ 与 HashJoin；Scan 提供 PhysicalScan。Cost 只使用行数：

```text
Scan = rows
NLJ  = left × right
Hash = left + right
```

验收标准：两表等值 Join 总能选 HashJoin；非等值 Join 回退 NLJ；手算与 Explain 一致。

### 第 5 步：实现 OptimizeGroup / Expr / Inputs

先不剪枝，完成完整动态规划；然后加入：

- Fired Rule Set；
- Task Cycle Detection；
- Current Winner；
- Upper Bound；
- Branch-and-Bound。

每加入一种状态，都要在 Trace 中可见。否则当一条候选消失时，无法判断是没匹配、被去重、被预算截断，还是被 Cost 剪掉。

### 第 6 步：加入 Logical Property

优先实现 Schema 和 Column Lineage。让每条 Rule 在 Debug 模式验证：

```text
output_schema(new_expr) == group.schema
output_lineage(new_expr) == group.lineage
```

验收标准：故意写错 Join Commute 列索引时，属性检查立即失败，而不是等执行结果错误才发现。

### 第 7 步：把 Required Physical Property 纳入搜索 Key

先只实现 Ordering：

```text
RequiredOrdering = Any | Prefix(columns)
```

让 Sort 作为 Enforcer，MergeJoin/SortAgg 可以产生或消费 Ordering。此时同一 Group 应出现多个 Winner：无序最优与有序最优不一定相同。

验收标准：父节点要求排序时，优化器能够在“孩子保序实现”与“无序低成本实现 + Sort”之间比较总代价。

### 第 8 步：再加入 Distribution、统计与反馈

在单机 Ordering 正确后，再扩展：

- Single、Hash(keys)、Broadcast Distribution；
- Exchange Enforcer 与网络 Cost；
- NDV、Null Count、Min/Max、Histogram；
- Join Key 相关性与多列统计；
- Actual Cardinality Collector；
- Winner 失效与 Memo 复用。

这个顺序可以避免最常见的自研陷阱：统计模型看起来很丰富，但搜索 Key 仍不正确，最终不同物理要求下的 Winner 被错误覆盖。

## 十五、从教学原型到生产优化器：源码暴露的演进清单

### 1. Required Properties 必须成为一等搜索状态

这是最高优先级。需要同时修改：

- `GroupInfo`：单 Winner → `PropertySet → Winner`；
- `SearchContext`：加入 Required Property；
- `TaskDesc`：避免把不同物理要求错误去重；
- `OptimizeInput`：属性 Passthrough、Derive、Satisfy、Enforce；
- Cost：Exchange、Sort、Materialize 等 Enforcer 成本；
- Plan Extraction：按 Root Required Property 提取。

### 2. 搜索调度需要 Promise，而不只是注册顺序

当前规则按 Vec 顺序扫描。生产优化器通常需要估计 Rule 的收益与扩张风险：

```text
高 Promise：实现规则、明显减少基数的 Filter Pushdown
中 Promise：有选择性依据的 Join Reorder
低 Promise：可能产生大量对称表达式的交换/结合
```

在预算有限时，Rule Ordering 本身就是计划质量的一部分。两阶段优化已经是粗粒度 Promise；下一步可以把优先级下沉到 Rule/Binding/Task。

### 3. Memo 需要高效 Parent Index 与增量失效

当前 Group Merge 会扫描并重写所有表达式，适合小规模教学。生产实现需要：

- Group/Expr Canonicalization；
- Parent MExpr Index；
- Union-Find 或等价的稳定重定向；
- Winner Dependency Graph；
- 统计或 Cost 变化后的精确失效；
- 内存预算与冷候选回收。

### 4. Binding 枚举需要 Lazy 与上限

当前嵌套 Matcher 对 Child Group 结果做笛卡尔积并一次性生成 Vec。可以演进为：

```text
Lazy Binding Iterator
  → 每产生一个 Binding 就评估 Promise/预算
  → 可暂停、可恢复
  → 超过阈值时保留最有希望的子集
```

否则 Group 内候选一多，Matcher 自身就可能先耗尽内存，而不是 Costing 成为瓶颈。

### 5. Cost 不应只是一组固定公式

生产模型至少要显式表达：

- CPU：表达式复杂度、Hash、Compare、Serialize；
- I/O：远端/本地、压缩后字节、Cache Hit；
- Network：Shuffle/Broadcast 字节与节点数；
- Memory：Hash Table、Sort Spill、并发 Pipeline；
- Latency：Blocking Boundary、并行度、Startup Cost；
- Risk：Cardinality 不确定性与资源超限概率。

`weighted_cost` 可以保留多维向量，再根据 Workload/SLA 选择权重。更进一步，Winner 不一定只有一个标量最优，可以保留 Pareto Frontier，延迟到资源环境明确时再决策。

### 6. Rule 正确性需要系统化验证

建议至少加入：

1. Property Invariant：Schema、Nullable、Keys、Lineage 不变；
2. Differential Test：随机小表上执行原计划与改写计划，比较结果；
3. Three-valued Logic Test：Null、Outer Join、NOT IN、Mark Join；
4. Rule Pair Convergence：检测规则组合产生的循环与爆炸；
5. Memo Replay：保存 Rule Trace，能够重放某次计划生成过程。

优化器最危险的 Bug 不是选慢计划，而是产生错误结果。Cost 可以近似，等价性不能近似。

### 7. Adaptive 需要稳定签名与并发模型

GroupId 只在当前 Memo 生命周期内有效。若要做跨查询 HBO，需要把反馈绑定到稳定语义签名，例如：

```text
Normalized SQL / Plan Fingerprint
  + Logical Group Digest
  + Predicate Parameter Bucket
  + Catalog/Schema Version
  + Statistics Version
```

同时要处理多分区聚合、采样偏差、并发更新、反馈衰减、参数敏感计划和版本失效。optd 的 `(GroupId → rows)` 是理解闭环的最小实现，但生产 HBO 的主要难点恰好在 GroupId 之外。

## 十六、如何阅读与调试 optd

如果希望沿源码亲手走一遍，推荐顺序如下：

```text
1. optd-core/src/nodes.rs
   先理解 PlanNode、PredNode、PlanNodeOrGroup

2. optd-core/src/cascades/memo.rs
   跟 add_new_group_expr_inner、append_expr_to_group、merge_group_inner

3. optd-core/src/cascades/rule_match.rs
   看 Pattern 如何从 Group 枚举 Binding

4. optd-datafusion-repr/src/rules/joins.rs
   用 JoinCommute、JoinAssoc、HashJoin 验证规则契约

5. optd-core/src/cascades/tasks2.rs
   按 OptimizeGroup → Expr → Rule → Input 追搜索状态

6. optd-datafusion-repr/src/cost/base_cost.rs
   手算 Scan、Join、Sort 的 Winner

7. optd-datafusion-repr/src/lib.rs
   看 Rule Set、预算、两阶段与 Adaptive Clear

8. optd-datafusion-bridge/src/{into_optd,from_optd,physical_collector}.rs
   最后串起 DataFusion 与运行时反馈
```

optd 的 Planner Test 支持输出 Physical Plan、Logical Join Orders、Memo Table 和 Rule Trace。一个高价值调试循环是：

```text
写一条两表/三表 SQL
  → 打开 dump_memo_table + enable_tracing
  → 找 Root Group
  → 列出该 Group 的 Logical/Physical MExpr
  → 找每次 apply_rule 的来源与产物
  → 手算 proposed Winner
  → 打开/关闭 pruning 比较搜索空间
  → 限制 logical_rules 验证某条规则的边际作用
```

不要只看最终 Explain。最终树隐藏了绝大多数被拒绝候选，而优化器的价值与问题通常都发生在“为什么某个候选没有成为最终树”这一层。

## 十七、重新理解 Optimizer 的本质

完成这条源码链路后，可以把现代查询优化器概括为五个相互约束的系统：

```text
语义系统
  Rule 只生成合法等价表达式

状态系统
  Memo 保存 Group、MExpr、Property 与搜索历史

搜索系统
  Task、Promise、Budget、Upper Bound 决定探索顺序与范围

评价系统
  Statistics + Cost 决定已探索候选中的 Winner

反馈系统
  Runtime Metrics 让 Statistics 失效并重算 Winner
```

任何一层单独变强，都不等于拥有一个更好的优化器：

- 规则很多，但 Memo 去重和预算差，会让规划时间爆炸；
- Memo 很完整，但 Required Property 不在 Key 中，会错误复用 Winner；
- 统计很精细，但 Transformation Rule 没生成好 Join Order，Cost 无从选择；
- Cost 公式复杂，但等价规则破坏 Null 语义，会得到“低成本的错误结果”；
- 运行时反馈很丰富，但没有稳定 Plan/Group 身份，只能形成一次性指标。

optd 最有价值的地方，正是把这些层次以较少代码放在同一条可运行链路中。它当前不是功能完备的生产 Optimizer：Physical Property 尚未进入 Cascades 搜索，Memo 与 Matcher 偏朴素，Hash Join、Distribution 和 Adaptive Collector 都有明确限制。但也正因为这些边界清晰，我们可以看到每增加一项现代能力，究竟应该改动哪一个抽象，而不是把所有优化都继续写成更多 Rule。

如果只能保留一句话，我会这样描述 Cascades：

> **它不是寻找一棵更好的树，而是在物理约束和搜索预算下，持续维护“同一语义有哪些实现、目前哪一个最便宜”的可复用证明。**

## 参考资料

- [optd 源码快照 e31c8e5](https://github.com/cmu-db/optd/tree/e31c8e5779538916dca04fad253fdb4d2bdf5ea7)
- [The Columbia Optimizer Thesis](https://15721.courses.cs.cmu.edu/spring2019/papers/22-optimizer1/xu-columbia-thesis1998.pdf)
- [How Good Are Query Optimizers, Really?](https://15721.courses.cs.cmu.edu/spring2024/papers/16-costmodels/p204-leis.pdf)
- [Adaptive Optimization of Very Large Join Queries](https://arxiv.org/abs/1902.08291)

---
title: "【源码】深入 StarRocks Optimizer：Enforcer、Property 级联与 Cost 选型"
date: 2026-09-01T00:00:00+08:00
lastmod: 2026-09-01T00:00:00+08:00
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
description: "结合 StarRocks 源码，分析 QueryOptimizer 全生命周期、Cascades 任务调度、Property 级联、Enforcer 插入、Join 分发和聚合阶段选型，并澄清 Merge Join 与 Sort Aggregate 的实际决策边界。"
draft: false
---

在 Cascades Optimizer 中，选出一个物理算子只是决策的一半。父节点还会要求孩子提供特定的数据分布、排序或 CTE 属性；孩子已有的输出属性如果不能满足要求，优化器必须插入 Exchange 或 Sort，并把这些额外成本计入完整计划。只有把 **Operator、Required Property、Enforcer 与 Cost** 放在一起，所谓“最优物理计划”才有完整含义。

StarRocks 的优化器是一份很适合研究这一问题的生产级实现。它不只用 `Memo / Group / GroupExpression` 保存逻辑等价空间，还以 `PhysicalPropertySet` 为维度记录 Winner，通过显式任务栈完成需求下传、子计划优化、属性推导、Enforcer 插入和代价回传。与较精简的 Cascades 教学实现相比，这条链路更能解释分布式优化器为什么不能只保存“每个 Group 一个最佳计划”。

本文沿着 `QueryOptimizer.optimizeByCost()` 的生命周期，重点回答四个问题：

1. StarRocks 如何调度 Cascades 搜索任务，并在规划超时前持续收紧搜索上界？
2. Required/Output Property 如何在 Join、Aggregate 与 Scan 之间级联传导？
3. Exchange 与 Sort Enforcer 在什么时机插入，又如何参与同一个 Cost 闭环？
4. Join 实现、Join 分发、聚合阶段和 Sort Aggregate 分别在哪一层决策？

## 核心结论

1. **StarRocks 的搜索子目标是带 Property 的 Group，而不是单纯的 Group。** `Group.lowestCostExpressions` 以 `PhysicalPropertySet` 为 Key，同一关系语义可以分别保留满足不同 Distribution、Sort 与 CTE 要求的最低成本实现。
2. **Enforcer 不是搜索结束后的补丁。** `EnforceAndCostTask` 在评价每个物理表达式时推导孩子要求、递归取得孩子 Winner、验证输出属性，并把必要的 Exchange/Sort 代价纳入候选总成本。
3. **需求自顶向下传播，属性、统计与代价自底向上返回。** 任务栈通过“挂起自身—优化孩子—恢复执行”模拟协程，Upper Bound 则沿父子链逐层收紧。
4. **Join 的主要 CBO 竞争不仅是算子本体，还包括 Broadcast、Shuffle、Colocate 与 Bucket Shuffle 的完整代价。** Property 等价类与列序对齐决定上层是否能够复用下层分布，从而省掉 Exchange。
5. **当前默认 `join_implementation_mode=auto` 并不会让 Hash Join 与 Merge Join 同组竞价。** 当前源码的 Auto 模式只注册 Hash Join 与 Nested Loop Join；Merge Join 需要显式使用 `merge` 模式。它具备 Property 与 Cost 实现，但不能表述为默认的 Hash-vs-Merge 自动选择。
6. **Sort Aggregate 也不参与 Memo 内的 Hash-vs-Sort Cost 竞争。** Memo 内比较的是 Hash Aggregate 的一阶段、二阶段与多阶段形态；Sort Aggregate 是 CBO 提取之后基于存储有序性的确定性物理改写。

### 源码分析基线

| 项目 | 源码快照 | 日期 | 重点范围 |
|------|----------|------|----------|
| StarRocks | [0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 2026-03-30 | QueryOptimizer、Memo Task、Property、Enforcer、CostModel、Physical Rewrite |

本文结论对应这一确定源码快照。StarRocks 仍在快速演进，尤其 Join Implementation Rule 的默认注册集合、代价公式与后置物理规则可能继续变化，不应把本文描述外推为所有版本的固定行为。

## 一、优化器生命周期总览

入口：`QueryOptimizer.optimizeByCost()`，一条六阶段流水线：

```text
SQL Statement
  │  Analyzer / Transformer（AST → 逻辑算子树）
  ▼
┌──────────────────────────────────────────────────────────────┐
│ ① logicalRuleRewrite（RBO）                                   │
│    子查询解关联、谓词下推/推导、列裁剪、MV 文本改写、           │
│    分区裁剪、聚合下推、CTE inline、limit 合并……（数十组规则）  │
├──────────────────────────────────────────────────────────────┤
│ ② memo.init + deriveAllGroupLogicalProperty                   │
│    逻辑树 → Memo/Group/GroupExpression 结构                   │
├──────────────────────────────────────────────────────────────┤
│ ③ memoOptimize（CBO，Cascades 任务搜索）★ 本文核心            │
│    规则装载（join reorder 分级）→ 任务栈自顶向下搜索           │
├──────────────────────────────────────────────────────────────┤
│ ④ extractBestPlan(requiredProperty, rootGroup)                │
│    沿 (property → bestExpr → inputProperties) 链递归提取      │
├──────────────────────────────────────────────────────────────┤
│ ⑤ physicalRuleRewrite（物理树后处理）                          │
│    PhysicalDistributionAggOptRule（SortAgg/PerBucket）、       │
│    ExchangeSortToMergeRule、PruneShuffleDistributionNode、     │
│    低基数字典编码（AddDecodeNode）、JoinLocalShuffle 等         │
├──────────────────────────────────────────────────────────────┤
│ ⑥ dynamicRewrite + PlanValidate → PlanFragmentBuilder         │
│    物理树 → Fragment/Exchange，inputProperties 决定 join 分发  │
└──────────────────────────────────────────────────────────────┘
```

### 关键设计取舍

- **RBO 前置、CBO 收窄**：大部分确定性规范化（谓词下推、列裁剪等）在进入 Memo 前完成，让搜索空间尽可能小；需要保留备选的 Join Reorder、物理实现、聚合阶段以及部分 MV 改写继续留在 Memo 中搜索。
- **`requiredColumns` 全程携带**：列裁剪需求放进 `TaskContext`，贯穿整个搜索。
- **规则分级装载**（`QueryOptimizer.memoOptimize`）：
  - join 数 ≥ `cbo_max_reorder_node` → 关闭 CBO reorder；
  - \> `cbo_max_reorder_node_use_exhaustive` → 贪心 `ReorderJoinRule` + 只加交换律；
  - 小规模才加交换律 + 结合律穷举；
  - `CboTablePruneRule` 仅当 join 数 < 10 时注册。

---

## 二、Cascades 任务调度框架

### 2.1 数据结构

| 结构 | 职责 |
|---|---|
| `Memo` | Group 集合 + 表达式去重（`copyIn`）+ enforcer 注册表（`insertEnforceExpression`） |
| `Group` | 逻辑等价类；`lowestCostExpressions: Map<PhysicalPropertySet, (cost, GroupExpression)>` 每个 property 维度各记一个最优解；`costLowerBounds` 记代价下界；`statistics` |
| `GroupExpression` | 一个算子 + 子 Group 指针；记录 `(outputProperty → inputProperties)`、`(property, cost)`、`appliedRules` 位图 |
| `TaskContext` | `requiredProperty` + `requiredColumns` + `upperBoundCost`（代价上界，初始 `MAX_VALUE`） |
| `PhysicalPropertySet` | Distribution + Sort + CTE 三元组 |

### 2.2 五类任务与调用链

`TaskScheduler` 用 `Stack<OptimizerTask>` 做自顶向下深度优先搜索，每弹一个任务调 `checkTimeout()` 防超时：

```text
OptimizeGroupTask(G, req)
 ├─ 剪枝：costLowerBound(req) >= UB 或 hasBestExpression(req) → return
 ├─ 每个逻辑表达式 → OptimizeExpressionTask      （后压，后执行）
 └─ 每个物理表达式 → EnforceAndCostTask          （先压，先执行）
        │
        ├─ RequiredPropertyDeriver → 子节点候选需求组合
        ├─ 子 Group 尚无 best expr？
        │    → pushTask(clone())                    ← 把自己挂起在栈里
        │    → pushTask(OptimizeGroupTask(child, childReq, UB - curTotalCost))
        │    → return（先优化子树）                  【递归下降】
        ├─ 子 Group 有 best expr → 累加代价；超上界即放弃
        ├─ 全部子节点就绪 → ChildOutputPropertyGuarantor + OutputPropertyDeriver
        └─ recordCostsAndEnforce → 登记 best / 不满足则插 enforcer
```

- `OptimizeExpressionTask`：收集 transform + implement 规则，**按 `rule.promise()` 排序**（implement 规则 promise=2 > transform 规则 promise=1，物理实现先行以尽快建立代价上界）；依次压入 `ApplyRuleTask × N`、`DeriveStatsTask`、每个子 Group 的 `ExploreGroupTask`。
- `ApplyRuleTask`：Binder 模式匹配 → `rule.check` → `rule.transform` → `memo.copyIn` 去重；`rule.exhausted()` 防死循环/耗时失控；新逻辑表达式压 `OptimizeExpressionTask`，**新物理表达式直接压 `EnforceAndCostTask`**。
- `DeriveStatsTask`：`StatisticsCalculator` 推导统计，幂等标记 `isStatsDerived`。

### 2.3 "自顶向下"的准确含义

**需求自顶向下传播，代价自底向上累积**。`EnforceAndCostTask` 通过 `clone()` + 任务栈模拟协程挂起/恢复：栈即调用栈，`curChildIndex / prevChildIndex / curPropertyPairIndex / curTotalCost` 即栈帧。每次挂起时子任务拿到 `UB - curTotalCost`，代价上界沿树逐层收紧——先找到的可行解越好，后续分支被剪得越狠（Branch-and-Bound）。

---

## 三、Property 的生成与级联传导

### 3.1 三个推导器

| 类 | 方向 | 职责 |
|---|---|---|
| `RequiredPropertyDeriver` | 自顶向下 | 算子"对子节点的需求候选组合"（interesting properties） |
| `OutputPropertyDeriver` | 自底向上 | 根据子输出 + 算子自身语义推导输出属性 |
| `ChildOutputPropertyGuarantor` | 横向兜底 | 子输出组合不合法时修正（colocate/bucket-shuffle 转换） |

### 3.2 Required property 的生成

StarRocks **不做任意 property 枚举**，候选来源只有两类：父节点传下来的 `requirementsFromParent` + 算子自身语义的有限候选集。典型规则：

| 算子 | 子需求候选 |
|---|---|
| Hash Join | 方案A `[ANY, BROADCAST]`；方案B `[hash(leftEq), hash(rightEq)]`（有等值列时）；`JoinHelper.onlyBroadcast/onlyShuffle` 按类型与 hint 裁剪 |
| Merge Join | 同 Hash Join，但**两侧额外要求按等值列的 SortProperty** |
| NestLoop Join | `[ANY, BROADCAST]`；right/full outer 双侧 `GATHER` |
| Hash Agg（GLOBAL） | 无 group by → `GATHER`；有 → `hash(groupBy)` SHUFFLE_AGG；LOCAL → `EMPTY` |
| Window | `sort(partitionBy+orderBy)` + 按 partition 列分布（无则 GATHER） |
| Limit / AssertOneRow | `GATHER` |
| CTE Anchor/NoCTE | 携带 `CTEProperty`（已用 CTE id 集合）做正确性约束 |

两个级联对齐机制（`PropertyDeriverBase`）：

1. **列序对齐**：`computeShuffleJoinRequiredProperties` 若父需求的 `SHUFFLE_JOIN` 列集与本层等值列相同，按父列序调整本层 shuffle 列——下层分布可被上层直接复用，省一层 exchange。`computeAggRequiredShuffleProperties` 对聚合同理（`shouldAdjustGroupByOrder`），并支持 `canRelaxGroupByCols` 的 null-relax 放宽。
2. **等价类并查集**：`HashDistributionSpec` 携带 `EquivalentDescriptor`，join 时把等值列做 `unionDistributionCols`（inner）/`unionNullRelaxCols`（outer）。这使得"按 `t1.a` shuffle 的输出"可以满足上层"按 `t2.b` 分布"的需求（当 `t1.a = t2.b` 是连接条件）——**分布属性穿越 join 向上传导**的核心。

### 3.3 Output property 的推导（`OutputPropertyDeriver`）

- broadcast join → 输出 = 左孩子属性（右孩子被复制，无分布信息）；
- shuffle join → 按主导侧（inner 取左、right join 取右、full outer 取左侧的 null-relax 版本）构造 `SHUFFLE_JOIN` 分布 + 等价类合并；
- OlapScan → `LOCAL` 分布 + 表/分区等价信息（colocate 识别基础）；
- Hash Agg / Project / Filter / Limit → 透传子属性；
- TopN(FINAL) → 输出 SortProperty（+ split 时 GATHER）；
- spill / partition join 开启时 `resetSortProperty`（落盘破坏顺序）。

### 3.4 级联传导全景图

```text
                  requiredProperty 自顶向下
    root[EMPTY / sort+gather]
        │ RequiredPropertyDeriver
        ▼
    agg[EMPTY] ──需求──> hash(groupBy) SHUFFLE_AGG
        │                     │ 列序对齐父层
        ▼                     ▼
    join ──需求──> [ANY, BROADCAST] 或 [hash(l), hash(r)]
        │                     │
        ▼                     ▼
    scan ──输出──> LOCAL(分布列, equivDesc)   outputProperty 自底向上
        ▲                     │
        └── equivDesc 并查集：t1.a = t2.b 使分布属性穿越 join 向上传导
```

---

## 四、Enforcer：触发过程与执行协议

### 4.1 物理形态

Enforcer 就是普通物理算子（`PhysicalProperty.appendEnforcers`）：

```java
// DistributionProperty.appendEnforcers
new GroupExpression(new PhysicalDistributionOperator(spec), Lists.newArrayList(child));
// SortProperty.appendEnforcers
new GroupExpression(new PhysicalTopNOperator(spec, ...), Lists.newArrayList(child));
```

分布不满足 → `PhysicalDistributionOperator`（翻译为 Exchange：shuffle/broadcast/gather/round-robin）；顺序不满足 → `PhysicalTopNOperator`（Sort）。

### 4.2 触发入口（全代码库仅两处 + 一个自恢复路径）

| 触发点 | 时机 |
|---|---|
| `OptimizeGroupTask.execute()` | Group 被（重新）优化时，对**存量**物理表达式逐个压入（同一物理算子在不同需求下反复估价） |
| `ApplyRuleTask.execute()` | implement 规则**新生成**物理表达式时立即压入（携带当前需求上下文 + 代价上界） |
| `clone()` 自恢复 | 子 Group 优化完成后从断点续跑（不算新触发） |

为什么新物理表达式必须立刻压入：逻辑表达式无代价，物理表达式的代价只能在"需求上下文"下计算（子需求取决于父需求）；且它错过了 `OptimizeGroupTask` 的存量快照，若不自己压任务就会漏估。

**注意**：enforcer 本身不会再触发 `EnforceAndCostTask`——其代价在 `CostModel.calculateCost(enforcer)` 就地结算后直接登记。

### 4.3 执行协议（`EnforceAndCostTask.execute` 四步）

```text
1. initRequiredProperties：RequiredPropertyDeriver 得候选需求组合列表
2. 逐候选、逐子节点优化（状态机，可挂起/恢复）：
   - 查 childGroup.getBestExpression(childReq)；没有 → 挂起自己 + 压子任务
   - 累加子代价，任何时刻超上界 → recordLowerBoundCost 放弃该候选
   - 熔断：canGenerateOneStageAgg / checkBroadcastRowCountLimit
3. 全部子节点就绪：
   - ChildOutputPropertyGuarantor 保证组合合法（可能补插 bucket-shuffle 等）
   - OutputPropertyDeriver 计算输出属性
4. recordCostsAndEnforce：
   - setPropertyWithCost 登记 (requiredProperty → cost, inputProperties)
   - outputProperty.isSatisfy(requiredProperty)？
       满足 → 直接登记（一个表达式可同时满足多个 requirement）
       不满足 → enforceProperty 插 enforcer：
         只缺分布且无顺序要求      → enforceDistribute
         只缺分布且有顺序要求      → 先清空旧属性再 Sort+Distribution
                                     （防止 [order,gather]→[order,shuffle] 死循环）
         只缺顺序                  → enforceSort
         都缺                      → GATHER 需求先 Sort 后 Gather；否则先 Distribution 后 Sort
   - curTotalCost < UB → 收紧上下文上界（全局剪枝发动机）
```

### 4.4 上界/下界剪枝闭环

- `OptimizeGroupTask` 入口：`costLowerBound(req) >= upperBoundCost` → 直接返回；
- `EnforceAndCostTask` 每累加一个子代价都检查超上界 → `recordLowerBoundCost` 并放弃；
- 子任务被上界剪掉（恢复后 `childBestExpr == null`）→ 父任务记 `costLowerBound = UB + 1` 退出。

---

## 五、一个完整示例：Optimizer 生命周期走读

```sql
SELECT t1.a, count(*) FROM t1 JOIN t2 ON t1.a = t2.b GROUP BY t1.a;
```

假设 `t1` 按 `a` 列 hash 分布（可本地满足 `hash(a)`），`t2` 分布与 `b` 无关。

### 5.1 Memo 结构（RBO 后）

```text
G0: LogicalAgg(group by a)            → children: [G1]
G1: LogicalJoin(t1.a = t2.b)          → children: [G2, G3]
G2: LogicalScan(t1)
G3: LogicalScan(t2)
```

### 5.2 任务时序（按栈顶执行顺序）

**阶段 1：从根下降到叶子**

| # | 任务 | 动作 |
|---|---|---|
| 1 | `OptimizeGroupTask(G0, EMPTY, UB=MAX)` | 压入 `OptimizeExpressionTask(G0)` |
| 2 | `OptimizeExpressionTask(G0)` | 压入 `ApplyRuleTask`、`DeriveStatsTask`、`ExploreGroupTask(G1)` |
| 3 | `ExploreGroupTask(G1)` | 探索 join 交换律等逻辑变换 |
| 4 | `DeriveStatsTask(G0)` | 推导统计 |
| 5 | `ApplyRuleTask(AggImplementRule)` | 生成 `PhysicalHashAggregate` → 压入 `EnforceAndCostTask(G0-Agg)` |
| 6 | `EnforceAndCostTask(G0-Agg)` | GLOBAL agg → 子需求 `[[hash(a) SHUFFLE_AGG]]`；G1 无 best → **挂起 + 压 `OptimizeGroupTask(G1, hash(a), UB-aggCost)`** |
| 7 | `OptimizeGroupTask(G1, hash(a))` | 压 join 的 `OptimizeExpressionTask` |
| 8 | `ApplyRuleTask(JoinImplementRule)` | 生成 `PhysicalHashJoin` → 压 `EnforceAndCostTask(G1-Join)` |
| 9 | `EnforceAndCostTask(G1-Join)` | 候选：A `[ANY,BROADCAST]`、B `[hash(a),hash(b)]`；先走 A，G2 无 best → 挂起，下钻叶子 |

**阶段 2：叶子产出 best（代价自底向上）**

| # | 任务 | 动作 |
|---|---|---|
| 10 | `EnforceAndCostTask(G2-ScanT1, ANY)` | 输出 `LOCAL(a)`，满足 ANY → `G2.best[ANY]=scanT1`；**收紧上界** |
| 11 | `EnforceAndCostTask(G3-ScanT2, BROADCAST)` | scan 不满足 BROADCAST → `enforceDistribute` 插 broadcast exchange，代价 = scan + broadcast，登记 `G3.best[BROADCAST]` |

**阶段 3：Join 逐方案收敛**

| # | 任务 | 动作 |
|---|---|---|
| 12 | `EnforceAndCostTask(G1-Join)` 恢复 | 两子就绪：Guarantor（broadcast 合法）→ OutputDeriver（输出 = 左孩子 `LOCAL(a)`）→ 满足 `hash(a)` → 登记 `G1.best[hash(a)] = Join(broadcast)` |
| 13 | 方案 B（shuffle） | 重置状态重估：G2 的 `LOCAL(a)` 直接满足 `hash(a)`（免 shuffle）；G3 需插 shuffle enforcer；若 colocate 则零 shuffle。代价竞争取低者 |

**阶段 4：回到根并提取**

| # | 任务 | 动作 |
|---|---|---|
| 14 | `EnforceAndCostTask(G0-Agg)` 恢复 | `G1.best[hash(a)]` 存在 → 累加；agg 输出透传；满足根需求 `EMPTY` → 登记 `G0.best[EMPTY]` |
| 15 | `extractBestPlan(EMPTY, G0)` | 沿 `inputProperties` 递归：Agg → `G1.best[hash(a)]`（inputProps `[ANY, BROADCAST]`）→ scanT1 / broadcast(scanT2) |

最终计划：

```text
HashAggregate (group by a)
  └─ HashJoin (broadcast)
       ├─ OlapScan(t1)
       └─ Exchange(BROADCAST)          ← enforcer 产物
            └─ OlapScan(t2)
```

若 shuffle 方案总代价更低（右表大、左表按 a 分布可免一侧交换），则提取为：

```text
HashAggregate (group by a)              ← 其 SHUFFLE_AGG(hash(a)) 需求
  └─ HashJoin (shuffle, 输出 SHUFFLE_JOIN(a))   ← 经列序对齐 + 等价类，免掉聚合侧交换
       ├─ OlapScan(t1)                  ← LOCAL(a) 直接满足 hash(a)
       └─ Exchange(SHUFFLE hash(b))
            └─ OlapScan(t2)
```


---

## 六、Join 的实现与分发：Cost 能决定什么，不能决定什么

### 6.1 Implementation Rule 的能力与注册方式

StarRocks 当前包含三条 Join Implementation Rule：

| 规则 | 物理算子 | `check()` 准入 |
|---|---|---|
| `HashJoinImplementationRule` | `PhysicalHashJoinOperator` | 非 cross join **且有等值谓词** |
| `MergeJoinImplementationRule` | `PhysicalMergeJoinOperator` | **存在等值谓词** |
| `NestLoopJoinImplementationRule` | `PhysicalNestLoopJoinOperator` | 没有等值谓词，并检查 Join Type 与功能开关 |

但“代码中存在三条规则”不等于“三种实现会同时进入同一 Group 竞价”。`QueryOptimizer.memoOptimize` 根据 `join_implementation_mode` 注册规则：

| 模式 | 实际注册的实现规则 | 含义 |
|------|--------------------|------|
| `auto`（默认） | Hash Join + Nested Loop Join | 等值连接走 Hash，非等值/Cross 由 NLJ 兜底 |
| `hash` | Hash Join | 强制只生成 Hash Join 能处理的物理实现 |
| `merge` | Merge Join | 显式启用 Merge Join 路径 |
| `nestloop` | Nested Loop Join | 强制只生成 NLJ 能处理的物理实现 |

`RuleSet.addAutoJoinImplementationRule()` 中的 Merge Join 注册目前仍被注释，并保留 `TODO: implement merge join`。因此在本文源码快照中，标准 Auto 模式没有发生 Hash Join 与 Merge Join 的直接 Cost 竞争。当前 Cost 主要决定的是：

- 一个已注册 Join 实现采用哪组 Child Required Properties；
- Broadcast、Shuffle、Colocate、Bucket Shuffle 等分发形态的完整成本；
- 同一 Required Property 下，不同 Join Order 与子计划组合谁成为 Winner。

### 6.2 Hash 与 Merge 的 Property 需求差异

即使两者当前不会在 Auto 模式中直接竞价，`RequiredPropertyDeriver` 仍完整表达了两种算子的物理契约：

```text
HashJoin 候选：
  方案A: [ANY,                BROADCAST]
  方案B: [hash(leftEq),       hash(rightEq)]

MergeJoin 候选：
  方案A: [sort(leftEq),       BROADCAST + sort(rightEq)]
  方案B: [hash(leftEq)+sort(leftEq), hash(rightEq)+sort(rightEq)]
```

即：**Merge Join 的每个方案都比 Hash Join 多一对按等值列的 Sort 需求**。在 `merge` 模式中，如果子树没有现成有序性，优化器就会为孩子插入 Sort Enforcer，并把代价加入该 Merge Join 计划。

### 6.3 两套代价模型如何评价各自候选

**Hash Join**（`HashJoinCostModel`，执行模式由子节点实际输入属性反推）：

```text
CPU = buildCost + probeCost + joinOutput.computeSize
  BROADCAST: build = rightOutput                    // 每 BE 并行度 1 建全量 HT
  SHUFFLE:   build = rightOutput / parallelFactor   // 并行建局部 HT

probeCost = leftOutput × cachePenaltyFactor
  mapSize = keySize × rightRows
  BROADCAST: penalty = min(12, max(1, log(mapSize/100000)))
  SHUFFLE:   penalty = min(3,  max(1, log(mapSize/100000) - log(parallelFactor)))

MEM = BROADCAST ? rightOutput × beNum : rightOutput
```

**Merge Join**（`visitPhysicalMergeJoin`）：

```text
有等值谓词: CPU = (leftSize + rightSize/2), MEM = 0
无等值谓词: CPU = leftSize + rightSize,  MEM = rightSize × EXECUTE_COST_PENALTY × 100  // 接近判死
```

单看算子本体，Merge Join 的 CPU 公式是线性的；但它的计划总代价必须加上 Sort Enforcer。Sort Enforcer 以 TopN 物理算子表达，其代价包含输入 CPU、输出内存与输入网络量。如果两侧都要从零排序，Merge Join 路径的总成本会显著增加。

这里应避免一个错误推论：**两套公式同时存在，不代表当前默认模式会用它们完成 Hash-vs-Merge 自动选型。** `CostModel` 只会评价已经被当前 Rule Set 生成的物理表达式；候选没有被注册和生成，Cost 再精细也无法选择它。

最终折算统一走：

```text
realCost = cpu × 0.5 + memory × 2.0 + network × 1.5
```

### 6.4 Merge Join 路径如何复用已有顺序

在显式 `merge` 模式下，Merge Join 是否需要 Sort Enforcer，仍取决于孩子 Output Property 能否满足等值列排序要求：

```text
MergeJoin(leftKey=a, rightKey=b)
  ├─ child output 已满足 sort(a) → 直接复用
  └─ child output 不满足 sort(b) → 插入 Sort Enforcer
```

Property 复用仍然有价值：已有顺序可以避免重复排序，使强制 Merge Join 路径更便宜；但这属于 **Merge Join 方案内部的 Property/Enforcer 优化**，不是默认 Auto 模式下与 Hash Join 的胜负比较。

两个重要的相关机制：

- **`CostModel.visitPhysicalTopN` 的 ∞ 惩罚**：非 enforced、非 split 的单阶段全量 FINAL sort（即无 limit 的全局排序）被判 `CostEstimate.infinite()`——强制排序拆成两阶段（各节点局部排序 + gather 后归并），防止产生天价单阶段 sort；
- **`physicalRuleRewrite` 的 `ExchangeSortToMergeRule`**（CBO 后）：把 `Exchange(GATHER) 之上的 FINAL sort` 改写为 `PARTIAL sort → Exchange → FINAL sort(split)` 的两阶段归并排序。这里的“Merge”指分布式有序流归并，不是 `PhysicalMergeJoinOperator`。

### 6.5 NestLoop 与防御性代价

- `visitPhysicalNestLoopJoin`：`cpu = leftSize × rightSize × EXECUTE_COST_PENALTY`，cross join 再乘 `cross_join_cost_penalty`；right join 右表过大额外惩罚——保证它只在无等值条件时兜底；
- `EnforceAndCostTask.checkBroadcastRowCountLimit`：右表行数超 `broadcast_row_count_limit` 且左表不够小 → 硬性否决 broadcast 方案（不走代价）；
- `JoinHelper.onlyBroadcast()/onlyShuffle()`：right outer/anti 等类型裁剪候选集。

### 6.6 Join 分发的第三/四/五种形态：colocate / bucket-shuffle

除 broadcast/shuffle 两个候选外，`ChildOutputPropertyGuarantor` 在子输出就绪后把组合修正为更省的形态（不改 Memo 搜索结构，只改 enforcer 形态与代价）：

- 双侧 `LOCAL` 且 colocate 组对齐 → **colocate join，零 exchange**；
- 左 `LOCAL` + 右 `SHUFFLE`/`LOCAL` → `transToBucketShuffleJoin`：只按左表分布路由右表（单边交换）。

---

## 七、聚合选型：Memo 内阶段竞争与 Memo 外 Sort Aggregate

### 7.1 关键事实：聚合阶段拆分发生在 CBO 内部

`SplitTwoPhaseAggRule` / `SplitMultiPhaseAggRule` 注册在 `RuleSet` 构造器（transform 规则），在 Memo 搜索中把单阶段 `GLOBAL agg` 变形成 `GLOBAL(split) → LOCAL(split)` 两层，与原形态放进**同一个 Group 竞争**：

```text
G_agg:
  Expr1: GLOBAL agg                        ← 原始（一阶段）
  Expr2: GLOBAL(split) → LOCAL(split)      ← Split 规则生成（二阶段）
  Expr3: 多 distinct 时的三/四阶段形态      ← SplitMultiPhaseAggRule
```

`SplitTwoPhaseAggRule.check` 要求 `isGlobal() && !isSplit`（防循环）；`isSuitableForTwoStageDistinct` 用统计做预筛（`isTwoStageMoreEfficient`：group by 基数 × 系数 < 输入行数才值得拆）。

### 7.2 Hash Aggregate 的代价竞争（`visitPhysicalHashAggregate`）

基础代价 = `input.computeSize × factor`（cpu）+ `output.computeSize × factor`（mem），其上叠加三组政策性代价：

1. **`invalidOneStageAggCost` → ∞**：必须多阶段的场景却生成非 split 单阶段 GLOBAL，判死；
2. **`redundantTwoStageAggCost` → ∞**：Group 已有 `input → GLOBAL` 一阶段最优解时，`input → LOCAL(split) → GLOBAL(split)` 被判"多余的 local 步骤"——防止统计高估基数时选出"先本地无效聚合再全量交换"的坏计划；
3. **`factor = 0.1`（软奖励）**：`isSplit && LOCAL` 的 local 阶段代价打一折；配套 `setExchangeCostFactor` 把 local agg 上方的 exchange 代价也 ×0.1（注释：*"In most scenes, local agg → exchange → global agg is better than exchange → global agg"*）。

另有倾斜场景系数 `computeDataSkewPenaltyOfGroupByCountDistinct`（0.2 / 0.5 / 1.5）引导三阶段拆分。

### 7.3 任务层硬闸门：`canGenerateOneStageAgg`

代价之外，`EnforceAndCostTask` 在拿到子 best 后硬否决不稳健的单阶段全局聚合：统计未知 / 行数不准、含 distinct、多 group by 列（>1）——可被 `new_planner_agg_stage=1` 或单 BE local-shuffle 豁免。

### 7.4 聚合需求与 join 分布的级联复用

`visitPhysicalHashAggregate` 生成 `hash(groupBy) SHUFFLE_AGG` 需求时，`computeAggRequiredShuffleProperties` 会：

- 若父层（上层 join）要求 `SHUFFLE_JOIN` 且列集一致 → **按父列序调整 group by 的 shuffle 列**（`shouldAdjustGroupByOrder`）；
- `canRelaxGroupByCols`：父层列是 null-relax 时，本层也放宽——保证 outer join 之上的聚合不额外插一次交换。

反方向同样成立：聚合的 `SHUFFLE_AGG` 输出经等价类并查集可被上层 `SHUFFLE_JOIN` 需求满足（详见第五节示例的 shuffle 变体）。

### 7.5 Sort Aggregate：不是 Memo 算子，而是 CBO 后的执行方式替换

StarRocks **没有 `PhysicalSortAggregate` 算子**参与代价竞争；"sort aggregate" 是 `physicalRuleRewrite` 阶段 `PhysicalDistributionAggOptRule.UseSortAGGRule` 的执行层替换，当 `enable_sort_aggregate = true` 且满足全部条件时，把 `PhysicalHashAggregateOperator` 打上 `useSortAgg=true` 标记：

```text
准入条件（全部满足）：
  1. agg 是一阶段全局聚合（isGlobal && group by 非空）
  2. 孩子是 OlapScan
  3. 仅命中单个分区（多分区同 key 不在同 tablet，无法流式）
  4. 所有 group by 列都是表的 key 列（前缀对齐）
  5. group by 列恰好覆盖 schema 的 key 前缀
满足 → scan.setNeedSortedByKeyPerTablet(true)
      BE 按 tablet 内 key 有序输出，agg 退化为流式（无 hash 表、无内存风险）
```

同规则还处理 `enable_per_bucket_compute_optimize`：单阶段全局聚合 + scan 直连时打 `usePerBucketOptmize` 标记，按 bucket 分组执行（本质仍是 hash agg，但利用分桶局部性减少冲突）。

### 7.6 Hash Agg vs Sort Agg 的选型小结

| 维度 | Hash Aggregate | Sort Aggregate |
|---|---|---|
| 决策位置 | CBO 内（阶段拆分 + 代价竞争） | CBO 后 physicalRuleRewrite（条件满足即替换） |
| 决策依据 | 统计（基数/聚合率/倾斜）+ 代价权重 | 物理布局事实（key 列、单分区、单阶段） |
| 内存 | 有 hash 表，依赖阶段拆分与 spill 缓解 | 无聚合结构内存，天然抗高基数 |
| 依赖的 property | 分布（SHUFFLE_AGG/GATHER/EMPTY） | **有序性隐含在存储层**（按 key 有序读），不经 Memo 传导 |

关键差异：Sort Agg 的"有序性"来自存储引擎（tablet 内数据按 key 有序），而不是 Memo 里的 SortProperty——所以它不产生/消费 enforcer，CBO 对它是"盲"的，由后置规则做确定性判定。

---

## 八、总结：决策层次与 Property 级联的全景

```text
┌─ 硬排除（规则/任务层，不进代价）────────────────────────────┐
│ implement rule.check()、JoinHelper.onlyXxx、                │
│ checkBroadcastRowCountLimit、canGenerateOneStageAgg、CTE 校验 │
├─ 代价竞争（CBO 内，数字裁决）───────────────────────────────┤
│ Auto 模式下的 Join Order、Hash/NLJ 准入与分发方案            │
│ Merge 模式下 Merge Join 的 Property/Enforcer 组合           │
│ broadcast vs shuffle vs colocate/bucket（需求候选+enforcer） │
│ Agg 一阶段/二阶段/多阶段（Split 规则 + ∞ 代价 + 0.1 奖励）    │
│ realCost = 0.5·cpu + 2·mem + 1.5·net                        │
├─ 确定性后处理（CBO 后，条件替换）───────────────────────────┤
│ SortAgg / PerBucket 标记、两阶段归并排序、冗余 shuffle 裁剪、  │
│ 低基数字典编码                                                │
└─────────────────────────────────────────────────────────────┘
```

Property 级联是贯穿三层的主轴：

1. **向下**：`RequiredPropertyDeriver` 把父需求 + 算子语义合成有限候选组合，逐层下传（列序对齐父层以复用分布）；
2. **向上**：`OutputPropertyDeriver` 自底向上推导，等价类并查集让分布/有序性穿越 join、agg 向上传导；
3. **横向**：`ChildOutputPropertyGuarantor` 修正不合法组合，把"两个便宜的局部最优"缝合成一个合法的更优全局解（colocate / bucket-shuffle）；
4. **竞争落点**：一切形态差异（broadcast/shuffle、一阶段/二阶段、是否复用有序性）最终都折算为 enforcer 与算子代价之和，在 `Group.lowestCostExpressions` 里按 property 维度各留胜者，由 `extractBestPlan` 沿 `inputProperties` 链条还原成最终物理计划。

---

## 附：关键源码索引

| 主题 | 文件 |
|---|---|
| 优化器入口/流水线 | [QueryOptimizer.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) |
| 任务调度 | [optimizer/task](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/task) |
| Property 推导 | [RequiredPropertyDeriver.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/RequiredPropertyDeriver.java)、[OutputPropertyDeriver.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/OutputPropertyDeriver.java) |
| Enforcer 形态 | [DistributionProperty.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/DistributionProperty.java)、[SortProperty.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/base/SortProperty.java) |
| 代价模型 | [CostModel.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/CostModel.java)、[HashJoinCostModel.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/cost/HashJoinCostModel.java) |
| Join 实现规则 | [rule/implementation](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/implementation) |
| 聚合拆分规则 | [rule/transformation](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/transformation) |
| SortAgg/PerBucket | [PhysicalDistributionAggOptRule.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/PhysicalDistributionAggOptRule.java) |
| 两阶段归并排序 | [ExchangeSortToMergeRule.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/ExchangeSortToMergeRule.java) |

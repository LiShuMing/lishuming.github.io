---
title: "【源码】Dynamic Filter 穿越 Outer Join：StarRocks、Doris 与 Trino 的正确性边界"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
slug: "dynamic-filter-through-outer-join"
categories:
  - 数据库
tags:
  - Runtime Filter
  - Dynamic Filter
  - Outer Join
  - StarRocks
  - Apache Doris
  - Trino
description: "结合 StarRocks、Apache Doris 与 Trino 源码，分析 Runtime Filter/Dynamic Filter 的生成、传播、Outer Join NULL 语义、Null-Safe Equality 及 Producer-Consumer 正确性。"
draft: false
---

Runtime Filter（RF）或 Dynamic Filter（DF）通过 Build 侧摘要减少 Probe 输入，但收益依赖选择率、到达时间与 Join 语义：Hash Join 的 Build 侧先收集连接键，再把键值摘要发送给 Probe 侧 Scan，从而提前跳过不可能匹配的数据。

对 Inner Join 而言，这个过程相对直观；一旦计划中出现 Left、Right 或 Full Outer Join，问题就会发生根本变化：

> 父 Join 生成的 RF/DF 能否穿过下层 Outer Join？可以下推到保留侧、NULL 生成侧，还是两侧？需要满足哪些条件？

直觉上的“连接键相等，所以过滤器可以沿等值关系传播”并不充分。Outer Join 不只决定哪些行能够匹配，还会在未匹配时合成 NULL。下推过滤器可能改变一行的匹配状态，使原本的匹配行变成 NULL 补齐行；这个新结果又可能通过上层表达式，最终改变查询语义。

本文结合 StarRocks、Apache Doris 和 Trino 的源码，建立一套统一的正确性模型，并回答以下问题：

1. “当前 Join 自己生成过滤器”与“父层过滤器穿过当前 Join”为什么必须分开讨论？
2. Outer Join 的保留侧和 NULL 生成侧分别允许怎样的下推？
3. 普通等值 `=`、Null-Safe Equality `<=>` 与 `IS NOT DISTINCT FROM` 有什么差异？
4. 为什么 `slot`、`cast(slot)` 通常安全，而 `coalesce(slot, 0)` 可能改变结果？
5. StarRocks、Doris 和 Trino 分别如何实现传播与正确性保护？

## 问题边界：生成过滤器与穿越 Outer Join

### 源码版本与结论范围

| 项目 | 源码快照 | 日期 | 重点入口 |
|------|----------|------|----------|
| StarRocks | [0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 2026-03-30 | `JoinNode`、`RuntimeFilterDescription` |
| Apache Doris | [5202d06](https://github.com/apache/doris/tree/5202d06dd8feb3390ff32839227eeee89c345b57) | 2026-08-21 | `RuntimeFilterGenerator`、`RuntimeFilterPushDownVisitor` |
| Trino | [68dae09](https://github.com/trinodb/trino/tree/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c) | 2026-08-22 | `PredicatePushDown`、`DynamicFiltersChecker` |

本文结论以以上本地源码快照为准。RF/DF 相关代码变化较快，尤其是 Outer Join 和 Null-Safe Equality 的支持范围，阅读其他版本时应重新核对 Rule、Session Variable 与回归测试。

### 建立正确的心智模型

RF/DF 只能提前排除不影响结果的输入，原 Join 条件仍需执行；过滤器生成与跨算子传播必须分别判断。

#### RF/DF 是运行时旁路，不是新的 Join 条件

以 Hash Join 为例：

```text
                 Build Side
                     │
                     ▼
              Hash Table Build
                     │
              Build Key Domain
                     │
         Bloom / IN / MinMax / Bitset
                     │
             Merge / Broadcast
                     │
                     ▼
Probe Scan ── Runtime Filter ── Hash Join ── Original Join Predicate
```

设 Build 侧真实键集合为 `D_build`，过滤器接受的集合为 `D_filter`。安全过滤器必须满足：

```text
D_build ⊆ D_filter
```

也就是说，过滤器允许假阳性，却不能有假阴性：

- Bloom Filter 可以误放一行，原 Join 条件会再次校验；
- MinMax 可以放过区间中的无效值；
- IN Filter 必须包含完整 Build Key；
- 分布式 Build 未合并完成时不能把局部值域当成全局值域；
- 构建失败、超时或取消应 Fail-Open。

因此 RF/DF 的正确性基础不是“足够精确”，而是“绝不错误拒绝潜在匹配行”。

#### 生成与传播是两个不同问题

考虑：

```sql
SELECT *
FROM A
LEFT JOIN B ON A.k = B.k;
```

Left Join 的左侧是保留侧。即使 `A.k` 不在 `B.k` 的 Build Domain 中，A 行也必须以 `B.* = NULL` 的形式输出。因此，该 Join 不能用 B 构建的过滤器删除自己的 A 行。

但对下面的计划：

```text
        Parent Inner Join：P.x = C.k
                   │
        Child Left Join：A.k = B.k
```

Parent Join 生成的过滤器可能已经代表“最终结果必须满足”的条件。这个过滤器是否能继续穿过 Child Left Join，需要分析它引用哪一侧、表达式如何处理 NULL，以及能否沿 Child Join 的等值条件安全改写。

所以必须分别回答：

```text
问题一：当前 Outer Join 能否成为 RF/DF Producer？
问题二：其他 Join 生成的 RF/DF 能否穿过当前 Outer Join？
```

把两者混在一起，会得到“Left Join 不生成 RF，所以 RF 不能穿过 Left Join”这样的错误结论。

### Outer Join 的真正风险：改变匹配状态

删除一个原本匹配的右侧行，可能使 Left Join 新增补 NULL 行；上层谓词若接受这个 NULL，结果就会改变。

#### Preserved Side 与 Null-Generating Side

保留侧定义未匹配行是否仍需输出，NULL 生成侧定义哪些字段会被补齐，两者共同约束过滤方向。

| Join 类型 | 保留侧 | NULL 生成侧 |
|-----------|--------|-------------|
| Left Outer Join | 左侧 | 右侧 |
| Right Outer Join | 右侧 | 左侧 |
| Full Outer Join | 左右两侧 | 左右两侧 |
| Inner Join | 无补 NULL 语义 | 无 |

向保留侧下推一个本来就必须在 Join 上方成立的谓词，通常只是提前删除最终也会被拒绝的行。

向 NULL 生成侧下推则更危险。假设一条 B 行原本能与 A 匹配，过滤器提前删除 B 后，Left Join 不会简单地“少输出一行”，而会把对应 A 重新输出为：

```text
A columns + B columns = NULL
```

如果上层表达式能把这个 NULL 转换为可匹配值，新生成的行就可能通过上层 Join。

#### 为什么 `coalesce` 是关键反例

`coalesce` 能把补 NULL 转成普通值，使错误下推产生的新行通过父条件，因而可以区分只看键等价的实现。

```sql
SELECT *
FROM A
LEFT JOIN B ON A.k = B.k
INNER JOIN C ON coalesce(B.k, 0) = C.k;
```

假设：

```text
A.k = 1
B.k = 1
C.k = 0
```

原计划中：

```text
A LEFT JOIN B 产生 B.k = 1
coalesce(1, 0) = 0 为 FALSE
最终无结果
```

如果上层过滤器被错误地下推到 B 并提前删除 `B.k = 1`：

```text
A LEFT JOIN B 失去匹配，重新产生 B.k = NULL
coalesce(NULL, 0) = 0 为 TRUE
最终错误地产生一行
```

这说明问题不只是“NULL 会不会被过滤”，而是**下推是否改变 Outer Join 的匹配状态，以及由此生成的新 NULL 行能否通过原谓词**。

#### Null-Rejecting 与 Null-Propagating

下文将两个相关属性分开检查；具体传播规则需要结合父 Join、表达式与目标侧推导，不能将术语当作不带条件的充分证明：

- **Null-Rejecting**：输入为 NULL 时，父层比较不可能为 TRUE；
- **Null-Propagating**：表达式保持 NULL，满足 `e(NULL)=NULL`。

| Probe 表达式 | 是否通常 Null-Propagating | 进入 NULL 生成侧 |
|--------------|---------------------------|------------------|
| `b.k` | 是 | 普通拒绝 NULL 的父等值条件下可证明 |
| `cast(b.k AS BIGINT)` | 是 | Cast 保持 NULL 时可证明 |
| `b.k + 1` | 通常是 | 仍需引擎能证明函数 NULL 传播 |
| `coalesce(b.k, 0)` | 否 | 不安全 |
| `ifnull(b.k, 0)` | 否 | 不安全 |
| 自定义函数 | 未知 | 默认不应下推 |

只有白名单还不够。正确实现应把函数的 Nullability/Null-Propagation 语义纳入表达式属性，并结合父层比较是否允许 NULL 共同判断。

## 源码路径：双边传播、血缘直穿与有向推导

### StarRocks：双边优先的激进传播

StarRocks 对符合条件的普通等值键尝试双边传播，再按 Join 类型降为单边或就地消费；路径优先级不等于无条件允许穿透。

#### 哪些 Join 自己生成 RF

StarRocks 的入口是 [`JoinNode.buildRuntimeFilters()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java)。源码先按 Join Type 限制 Producer：

```java
if (!joinOp.isAnyInnerJoin()
        && !joinOp.isLeftSemiJoin()
        && !joinOp.isRightJoin()
        && !joinOp.isCrossJoin()) {
    return;
}
```

`child(1)` 是 Build 侧，生成的 RF 被推向 `child(0)` Probe 侧。Left/Full Outer Join 不走普通 RF 生成流程，因为其左侧包含必须保留的未匹配行；Right Outer Join 的左侧是非保留侧，可以使用右侧 Build Domain 预过滤。

每个 RF 还记录 Producer 等值条件是否允许 NULL：

```java
rf.setEqualForNull(
        BinaryPredicate.IS_EQ_NULL_PREDICATE.apply(joinConjunct));
```

[`RuntimeFilterDescription.equalForNull`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterDescription.java) 不是执行细节，而是 RF 穿越其他 Outer Join 时不可丢失的语义。

#### 双边、单边、就地接受

[`JoinNode.pushDownRuntimeFilters()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java) 使用三级策略：

```text
先尝试 Bilateral：沿 Join 等值列向两个 Child 传播
      ↓ 不适用
再尝试 Unilateral：按 Join Type 选择合法的一侧
      ↓ 未成功
最后在当前 Join 就地注册 Probe
```

主流程可以概括为：

```java
Optional<Boolean> result =
        pushDownRuntimeFilterBilaterally(context, probeExpr, partitionByExprs);
if (result.isEmpty()) {
    result = pushDownRuntimeFilterUnilaterally(
            context, probeExpr, partitionByExprs);
}
if (result.isPresent() && result.get()) {
    return true;
}
// 无法继续下推时，尝试在当前节点消费
```

这个设计体现了明确的性能倾向：只要能够证明安全，就尽量让同一个 RF 覆盖更多 Scan。

#### Bilateral：沿普通等值列复制到两侧

双边路径的前置条件包括：

- 当前不是 Cross Join；
- 当前不是 Null-Aware Left Anti Join；
- 当前存在等值 Join Conjunct；
- `probeExpr` 必须是直接 `SlotRef`；
- Probe Slot 必须参与当前 Join 的普通 `BinaryType.EQ`。

满足条件后，两侧都会尝试，而不是短路：

```java
boolean pushed =
        pushDownRuntimeFiltersForChild(context, probeExpr, partitionByExprs, 0);
pushed |=
        pushDownRuntimeFiltersForChild(context, probeExpr, partitionByExprs, 1);
```

假设父层 RF 的 Probe 是 `B.k`，子树为：

```text
A LEFT JOIN B ON A.k = B.k
```

候选表达式推导可以把同一个 Filter ID 分别落到：

```text
Scan(A)：A.k
Scan(B)：B.k
```

普通 `=` 拒绝 NULL，且 RF 对 Build Domain 无假阴性时，所有能参与父层匹配的非 NULL 等值键都必须通过过滤器，因此双边传播能够显著扩大过滤范围。

#### Unilateral：按 Join 类型限制方向

无法使用双边路径时，StarRocks 按当前 Join Type 选择单侧：

```text
Left Anti / Left Outer    → child(0)，只推左侧
Right Anti / Right Outer  → child(1)，只推右侧
Inner / Semi / Cross      → 依次尝试 child(0)、child(1)
```

单边路径在第一个成功的 Child 后停止。它与双边路径的语义不同：前者遵循保留方向，后者依靠普通等值列建立跨侧等价类。

#### NULL 补齐处理与一个需要回归验证的边界

[`JoinNode.checkRuntimeFilterOnNullValue()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java) 会记录 Outer Join 合成的哪些 NULL 列可被 RF 拒绝：

```java
if (joinOp.isOuterJoin()
        && !description.getEqualForNull()
        && slotRefWithNullValue) {
    filter_null_value_columns.add(slotId.asInt());
}
```

这段逻辑处理“RF 已经落在当前 Join 后，怎样对待合成 NULL”，但不能单独证明“RF 能否沿等值关系复制到另一侧”。

当前快照中的 `pushDownRuntimeFilterBilaterally()` 检查了**中间 Join 条件**必须是普通 `EQ`，函数内部却没有显式检查**父 RF** 的 `equalForNull`。因此下面的形态值得加入结果级回归：

```sql
(A LEFT JOIN B ON A.k = B.k)
INNER JOIN C ON B.k IS NOT DISTINCT FROM C.k;
```

当 `C.k` 包含 NULL 时，上层 RF 允许 NULL；未匹配 A 产生的 `B.k = NULL` 可能与 `C.NULL` 匹配。此时不能仅依据下层 `A.k = B.k`，把 `B.k` 上的 Null-Safe RF 无条件反向复制到 `A.k`。

这里应保持审慎表述：从当前函数局部看，`equalForNull` 与传播方向值得重点审计；是否形成实际错误计划，还要结合上游表达式改写、候选 Slot 推导及执行层 NULL 处理做完整回归验证。

### Doris：血缘直穿与等值扩展分离

原表达式沿血缘下推与跨连接键派生新表达式需要不同证明，Doris 将这两类行为分开检查。

#### Producer 拒绝列表

Doris Nereids 在物理后处理阶段由 [`RuntimeFilterGenerator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java) 生成 RF。以下 Join Type 默认不能成为 Producer：

```java
LEFT_ANTI_JOIN,
FULL_OUTER_JOIN,
LEFT_OUTER_JOIN,
ASOF_LEFT_OUTER_JOIN,
NULL_AWARE_LEFT_ANTI_JOIN
```

这同样只回答“当前 Join 是否自己生成 RF”，父层 RF 是否能穿过它由另一套 Visitor 决定。

#### 两种传播行为不能混为一谈

[`RuntimeFilterPushDownVisitor.visitPhysicalHashJoin()`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterPushDownVisitor.java) 把传播拆成两类。

第一类是**沿原表达式血缘直穿**：

```text
Probe Expression 引用哪个 Child 的 Slot
  → 检查能否穿过该 Child
  → 保持原表达式继续向下
```

第二类是**沿当前 Join 等值条件扩展**：

```text
Probe = leftExpr
  → 根据 leftExpr = rightExpr 生成 rightExpr
  → 创建新的 PushDownContext
  → 向对侧继续下推
```

这种拆分比“任何 Join 等值类都向两侧复制”更容易表达 Outer Join 的方向性。

#### 进入 NULL 生成侧必须保持 NULL

`canPushThroughJoinChild()` 先判断目标 Child 是否是当前 Join 的 NULL 生成侧：

```java
if (join.equals(ctx.builderNode)
        || !isNullGeneratingChild(join.getJoinType(), isLeftChild)) {
    return true;
}
return isNullPropagating(ctx.probeExpr);
```

当前源码认可：

- 直接 `Slot`；
- 保持 NULL 的 `Cast`；
- 子表达式同样满足条件的 `PropagateNullable` 函数。

`coalesce` 不满足这个条件，所以不能穿过 NULL 生成侧。

源码中的 [`RuntimeFilterTest`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/test/java/org/apache/doris/nereids/postprocess/RuntimeFilterTest.java) 直接固定了这条边界：

```java
select *
from lineorder
left outer join customer
  on lo_custkey = c_custkey
inner join supplier
  on coalesce(c_custkey, 0) = s_suppkey
```

测试期望 RF 数量为 0；将上层条件改成 `c_custkey = s_suppkey` 后，裸 Slot 保持 NULL，测试期望生成两个 RF Target。

结果级回归 [`test_runtime_filter_outer_join_nullable_side.groovy`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/regression-test/suites/correctness_p0/test_runtime_filter_outer_join_nullable_side.groovy) 进一步使用真实数据检查 `coalesce` 反例，而不是只比较 Explain 文本。

#### Null-Safe Equality 单独拦截

如果 Builder Join 的 Hash Conjunct 是 `NullSafeEqual`，Visitor 在遇到 Outer Join 时直接停止：

```java
if (equal instanceof NullSafeEqual
        && join.getJoinType().isOuterJoin()) {
    return false;
}
```

原因是普通 `=` 会拒绝合成 NULL，而 `<=>` 允许 NULL 参与匹配。把两者看成同一类 Hash Key，会破坏前述 Null-Rejecting 证明。

#### 等值扩展仅限 Inner/Semi Join

Doris 的跨侧 Expansion 需要同时满足：

- 当前 Join 不是 RF Builder；
- 打开 `expand_runtime_filter_by_inner_join`；
- 当前 Join 是 Inner 或 Semi Join；
- Probe Expression 能与某个 Hash Conjunct 的一侧精确匹配；
- 新 Target 只引用一个 Slot，且不会回到源表达式。

Outer Join 可以允许一个保持 NULL 的表达式沿自身血缘进入 NULL 生成侧，但不会被当作普通等价类向另一侧扩展。这是 Doris 与当前 StarRocks 路径最明显的设计差异。

#### Scan 落点仍有成本与能力约束

`visitPhysicalRelation()` 还要求：

- Scan 支持 Runtime Filter；
- Target 最终能够绑定到单个 Slot；
- 非数值类型避免在 Scan 期执行昂贵函数表达式；
- 同一 Target 去重；
- 分别登记 `targetExprIdToFilter`、`joinToTargetExprId` 和 `targetsOnScanNode`。

因此“逻辑上允许穿过 Join”不等于“最终一定生成 Scan RF”。表达式可计算性、数据类型、Connector 能力和 Pruner 仍会决定最终落点。

### Trino：把 DF 作为特殊谓词做有方向推导

Trino 的 Outer Join 推导限制了可改写的作用域，因此 DF 不能仅凭连接键等价就在两侧自由复制。

#### Left/Full Join 不自行生成 DF

Trino 的入口位于 [`PredicatePushDown.createDynamicFilters()`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/optimizations/PredicatePushDown.java)：

```java
if ((node.getType() != INNER && node.getType() != RIGHT)
        || !isEnableDynamicFiltering(session)
        || !dynamicFiltering) {
    return empty;
}
```

只有 Inner 和 Right Join 进入该路径。Right Join 的左侧是 Probe/非保留侧，所以可以使用右侧 Build Domain；Left/Full Join 不能生成过滤自身保留行的 DF。

对 `IS NOT DISTINCT FROM`，Trino 会显式构造 `nullAllowed=true` 的 Dynamic Filter 描述，确保 NULL 语义不会在后续阶段丢失。

#### `processLimitedOuterJoin()` 决定穿越方向

父 Join 产生的 DF 进入子 Left Join 时，被当作 `inheritedPredicate` 交给 [`processLimitedOuterJoin()`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/optimizations/PredicatePushDown.java)。

关键过程是：

```java
Expression outerRewritten =
        outerInference.rewrite(conjunct, outerScope);
if (outerRewritten != null) {
    outerPushdownConjuncts.add(outerRewritten);

    Expression innerRewritten =
            potentialNullSymbolInference.rewrite(
                    outerRewritten, innerScope);
    if (innerRewritten != null) {
        innerPushdownConjuncts.add(innerRewritten);
    }
}
else {
    postJoinConjuncts.add(conjunct);
}
```

这形成一条有方向的传播：

```text
父层 DF
  → 必须先能改写为 Left Join 保留侧表达式
  → 保留侧保留一份 Predicate
  → 再借助 Join Equality 派生 NULL 生成侧副本
```

安全性来自两个条件：

1. 原谓词已经下推并保留在 Outer/Preserved Side；
2. Inner/NULL-Generating Side 的副本只是进一步减少匹配候选。

即使 Inner 侧提前过滤导致匹配状态变化，Outer 侧原谓词仍会拒绝不满足 DF 的保留行，不会凭空增加最终结果。

#### 引用 NULL 生成侧时停留在 Join 上方

如果父 DF 直接引用子 Left Join 的右侧 Symbol，`outerInference` 不包含足以把它反向改写到 Outer Scope 的关系，`outerRewritten` 为 NULL，该谓词进入 `postJoinConjuncts`。

因此 Trino 的结论不是“父 DF 可以穿过 Left Join 的两侧”，而是：

> 只有能先表示为保留侧谓词的 DF，才允许沿 Join Equality 再派生到 NULL 生成侧；无法改写到保留侧的谓词停留在 Join 上方。

这比对 Outer Join 等值类做无条件双边复制更保守，也更直接地体现了传播方向。

#### Producer-Consumer 完整性校验

Trino 的 [`DynamicFiltersChecker`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/sanity/DynamicFiltersChecker.java) 会验证：

```text
当前 Join 生成的 DF 必须被 Probe 子树完整消费
当前 Join 的 Build 子树不得消费这些 DF
Join Filter 中不能遗留未下推的 DF 表达式
所有 Consumer 必须能匹配到某个 Join/SemiJoin Producer
```

Build 子树不能消费自己的 DF，不只是语义要求，也是在防止循环依赖：

```text
Build 完成才能产生 DF
    ↑              ↓
Build Scan 等待 DF 消费
```

这种计划永远无法取得进展。

[`RemoveUnsupportedDynamicFilters`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/RemoveUnsupportedDynamicFilters.java) 还会移除无法落到受支持 TableScan、表达式不合法或没有有效 Consumer 的 DF。规划期生成候选 Predicate，不代表它必须保留到最终计划。

## 将实现差异还原为正确性条件

### 三套系统的实现对比

三套系统用不同结构表示相同风险；比较应落到生成条件、传播方向与 NULL 处理，而不是按下推深度判断优劣。

| 维度 | StarRocks | Apache Doris | Trino |
|------|-----------|--------------|-------|
| 名称 | Runtime Filter | Runtime Filter | Dynamic Filter |
| 主要规划阶段 | 物理 PlanNode 构建/下推 | Nereids Plan Post Process | Predicate Pushdown/Iterative Rule |
| Left Join 自身生成 | 否 | 否 | 否 |
| Right Join 自身生成 | 支持 | 依 Join 拒绝集合与实现 | 支持 |
| 穿越策略 | Bilateral → Unilateral → Local | 血缘直穿与 Equality Expansion 分离 | 特殊谓词的 Outer → Inner 有向改写 |
| Outer Join 跨侧扩展 | 普通 EQ Join Key 可双边尝试 | Expansion 仅 Inner/Semi | 先落保留侧，再向 NULL 生成侧派生 |
| Null-Propagating 判断 | Bilateral 限制为 SlotRef | Slot/Cast/PropagateNullable | 通过 Predicate Scope 与 EqualityInference 控制 |
| Null-Safe 语义 | `equalForNull` | Outer Join 显式阻断 `NullSafeEqual` | `nullAllowed` |
| 最终落点 | PlanNode/Scan Target | PhysicalRelation + Context 索引 | Filter(TableScan) 等受支持 Consumer |
| 完整性保护 | Probe 注册与执行描述 | Context、Pruner、Translator | Remove Rule + Checker |

三者没有谁可以简单概括为“允许”或“不允许穿过 Outer Join”。差异主要来自正确性证明放在哪一层：

- StarRocks 更依赖 Join Key 与候选 Slot 的双边传播；
- Doris 把表达式 NULL 行为编码为 Visitor 的显式门槛；
- Trino 借助 Predicate Scope 和 Equality Inference 保持有向推导。

#### 正确之后还要有预算：Domain 不能无限增长

RF/DF 传播即使语义正确，也可能因为 Build Domain 太大而得不偿失。精确 Value Set 会消耗内存和网络，Bloom 会产生构建与 Probe CPU，MinMax 在离散分布上可能几乎没有过滤能力。Trino 的[官方 Dynamic Filtering 文档](https://trino.io/docs/current/admin/dynamic-filtering.html)明确设置 distinct-values、字节数和 range-row 等阈值；超过阈值后可能退化为 MinMax，而不是无限收集值集合。

这说明 Producer-Consumer 协议还需要一组资源状态：

```text
COLLECTING → COMPLETE(value-set / bloom / minmax)
           → TOO_LARGE（退化或禁用）
           → TIMEOUT（Fail-Open）
           → FAILED（Fail-Open）
```

Consumer 不应把“过滤器对象存在”解释成“过滤器有效且完整”。执行 Profile 至少要区分 Build Rows、NDV、Serialized Bytes、Merge Time、等待时间、最终表示、Consumer 数量、实际过滤行数和被跳过的 Split/Page。否则一个传播范围很广、过滤率接近零的 DF 可能只是在整个集群广播额外工作。

### 一套统一的正确性判据

对任意被下层 RF/DF 丢弃的输入行，必须证明：

> 它在未下推计划中可能产生的所有输出，都会在过滤器原来的语义位置被拒绝。

Outer Join 还需要额外证明：

> 提前过滤改变匹配状态后新产生的 NULL 补齐行，也不可能通过原来的过滤条件。

工程上可以拆成六个检查。

#### Producer 是否完整

只有覆盖相关 Build 范围的摘要才能据以丢弃 Probe 行；局部尚未完成的集合不能当作全局完整集合。

```text
过滤器是否覆盖完整 Build Domain？
分区过滤器是否已经完成 Merge？
超时或构建失败是否 Fail-Open？
```

#### 目标侧是否允许改变匹配候选

目标侧的保留语义决定删除是否安全，必须检查丢行是否产生新的补 NULL 输出。

```text
Preserved Side
Null-Generating Side
Inner/Semi Side
Anti/Null-Aware Anti Side
```

Anti Join 与 Null-Aware Anti Join 尤其不能套用普通 Inner Join 规则，因为删除 Probe 或 Build 行可能改变“未匹配”判断。

#### 父谓词是否拒绝 NULL

普通 `=` 通常拒绝 NULL；`<=>` 和 `IS NOT DISTINCT FROM` 允许 NULL 匹配。这个属性必须跟随过滤器传播，不能只看中间 Join Condition。

#### Probe 表达式是否保持 NULL

表达式若将 NULL 映射为可匹配值，过滤输入与过滤 Join 输出就可能不等价。

```text
e(NULL) = NULL       → 可能安全
e(NULL) = non-NULL   → 不能进入 NULL 生成侧
```

表达式是 SlotRef 只是一个保守的充分条件，不是理论上的必要条件。成熟实现可以基于函数元数据扩大到更多 Null-Propagating 表达式。

#### 等值传播是否有方向

Inner Join 的普通等值关系通常可以双向推导；Outer Join 应根据保留侧、NULL 生成侧与谓词原始位置建立有向传播，而不是把所有 Join Key 放进一个无方向等价类。

#### Producer-Consumer 是否闭环

消费者需要合法来源和可满足的执行依赖，否则过滤器可能永不到达、过早生效或形成等待环。

```text
Producer 存在
  → Probe Consumer 可达
  → Build 不消费自身过滤器
  → Scan 能计算 Target Expression
  → 无 Consumer 时删除 Producer
```

## 用反例、接口与执行依赖验证下推

### 推荐的回归测试矩阵

只检查 Explain 中出现 `runtime filter` 不足以验证正确性。至少需要三层测试：

| 层次 | 验证目标 |
|------|----------|
| Planner Unit Test | Producer 数量、Target 表达式、下推方向 |
| Explain/Shape Test | Filter ID、Build/Probe、Fragment 与 Scan 落点 |
| Result Regression | NULL、重复键、空 Build、匹配状态变化后的结果 |

#### Join Type

Join 类型决定未匹配行是否保留，测试需要覆盖下列类型与 Build/Probe 方向，而不是只测 Inner Join。

```text
INNER
LEFT / RIGHT / FULL OUTER
LEFT / RIGHT SEMI
LEFT / RIGHT ANTI
NULL-AWARE LEFT ANTI
```

#### Predicate

谓词测试必须区分普通等值、NULL 安全等值与可接受 NULL 的表达式，否则难以触达错误传播路径。

```sql
a.k = b.k
a.k IS NOT DISTINCT FROM b.k
cast(a.k AS BIGINT) = b.k
coalesce(a.k, 0) = b.k
ifnull(a.k, 0) = b.k
f(a.k) = b.k
```

#### 数据分布

空 Build、NULL、重复键和倾斜会激活不同执行分支，需要独立构造，不能只依靠均匀随机数据。

```text
Probe NULL / Build NULL
Probe 空 / Build 空
重复键
全匹配 / 全不匹配 / 部分匹配
Local RF / Global RF
过滤器部分到达、超时、取消
```

#### 必测反例

回归测试应验证匹配行被删后是否产生额外补 NULL 行，并同时确认目标传播路径实际被触发。

```sql
-- 非 Null-Propagating 表达式
SELECT *
FROM A
LEFT JOIN B ON A.k = B.k
INNER JOIN C ON coalesce(B.k, 0) = C.k;

-- Null-Safe 父 Join
SELECT *
FROM (A LEFT JOIN B ON A.k = B.k)
INNER JOIN C ON B.k IS NOT DISTINCT FROM C.k;

-- Full Outer Join 两侧都可能生成 NULL
SELECT *
FROM (A FULL OUTER JOIN B ON A.k = B.k)
INNER JOIN C ON A.k = C.k;
```

结果测试要刻意构造“提前删除原匹配行后产生 NULL 补齐行”的数据，否则很容易只覆盖性能路径，没有覆盖语义风险。

### 实现建议

生成、语义传播与 Scan 能力分层，可以避免把成本检查误当成正确性检查。以下接口是设计建议，不是三个项目共用的真实 API：

```text
canGenerate(joinType, predicate)
  → 当前 Join 能否成为 Producer

canPassThrough(joinType, childSide, probeExpr, filterSemantics)
  → 能否沿原血缘进入某个 Child

rewriteToChild(joinPredicate, sourceExpr, targetChild)
  → 能否安全改写到另一侧

canConsume(scan, targetExpr, filterType)
  → Scan 是否支持该落点

validate(producer, consumers)
  → 检查生命周期与依赖闭环
```

过滤器描述至少应携带：

```text
Filter ID
Producer Join/Fragment
Build Expression
Probe Expression
Comparison Type
Null Allowed / Equal For Null
Local / Global
Partition Mapping
Build Completeness / Ready State
Fail-Open State
```

这样，Join Type、NULL 语义、表达式能力和执行协议不会散落在互不知情的函数中。

### 下推前必须完成的证明

只检查 Probe 是否为 Join Key，不足以证明传播安全。实现还需确认过滤器的 NULL 语义、表达式的 NULL 行为，以及提前丢行是否改变 Outer Join 的匹配状态。Producer 完整性、Fail-Open、原始 Join Predicate 和合法 Consumer 缺一不可；测试应同时比较结果与计划，不能用下推深度代替正确性。

## 关键源码阅读索引

| 主题 | 项目 | 源码入口 |
|------|------|----------|
| RF 生成与三级下推 | StarRocks | [`JoinNode.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java) |
| RF NULL 语义描述 | StarRocks | [`RuntimeFilterDescription.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterDescription.java) |
| RF Producer | Doris | [`RuntimeFilterGenerator.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java) |
| RF 下推 Visitor | Doris | [`RuntimeFilterPushDownVisitor.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterPushDownVisitor.java) |
| Planner 单元测试 | Doris | [`RuntimeFilterTest.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/test/java/org/apache/doris/nereids/postprocess/RuntimeFilterTest.java) |
| NULL 生成侧结果回归 | Doris | [`test_runtime_filter_outer_join_nullable_side.groovy`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/regression-test/suites/correctness_p0/test_runtime_filter_outer_join_nullable_side.groovy) |
| DF 生成与 Outer Join 下推 | Trino | [`PredicatePushDown.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/optimizations/PredicatePushDown.java) |
| 删除不支持的 DF | Trino | [`RemoveUnsupportedDynamicFilters.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/RemoveUnsupportedDynamicFilters.java) |
| Producer-Consumer 校验 | Trino | [`DynamicFiltersChecker.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/sanity/DynamicFiltersChecker.java) |

本文基于钉钉文档 [《【调研】Dynamic Filter 下推能力调研》](https://alidocs.dingtalk.com/i/nodes/YMyQA2dXW7gYo6Mzc147ebrwWzlwrZgb) 重新整理，并结合三个项目的本地源码补充了统一正确性模型、实现差异和回归测试方法。

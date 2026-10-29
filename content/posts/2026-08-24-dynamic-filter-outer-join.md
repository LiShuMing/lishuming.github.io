---
title: "【源码】Dynamic Filter 穿越 Outer Join：StarRocks、Doris 与 Trino 的正确性边界"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
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
description: "Outer Join 下 RF/DF 穿越的正确性边界：统一判据 + StarRocks/Doris/Trino 对照；工程判断前置，压缩路径复述。"
draft: false
---

> 本系列第 3/3 篇 · Join 执行 · [上一篇]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}}) · 相关：[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}})

> **读完带走**
>
> - RF/DF 只能**无假阴性**地缩小 Probe；Outer Join 下真正的风险是**改变匹配状态后新产生的补 NULL 行**仍能通过上层谓词（典型反例：`coalesce`）。
> - **生成**与**穿越**必须分开判定：Left/Full 通常不当自己的保留侧 Producer，不代表父层 RF 不能穿过它。
> - 三系统对照：StarRocks 偏双边等值复制；Doris 分血缘直穿与 Inner/Semi 等值扩展，并显式拦 Null-Safe；Trino 要求先落到保留侧再有向派生。
> - 工程上优先用文末统一判据表做回归，而不是比“谁下推得更深”。

Runtime Filter（RF）/ Dynamic Filter（DF）用 Build 侧键摘要提前过滤 Probe。Inner Join 较直观；一旦计划含 Outer Join，关键问题变为：

> 父 Join 的 RF/DF 能否穿过下层 Outer Join？落在保留侧、NULL 生成侧，还是两侧？在什么条件下不会改语义？

“键相等即可沿等值类传播”不充分：提前删掉本可匹配的 NULL 生成侧行，会合成补 NULL 行；若上层表达式把 NULL 映射成可匹配值，结果就会变。

## 我的工程判断（先读）

**主张：** 下推安全 ⟺ 对被丢弃的每一行，未下推计划中它可能产生的全部输出，在原过滤位置仍会被拒绝；且因匹配状态变化新产生的补 NULL 行也不能通过原条件。  
**机制：** 分层检查 Producer 完整性、目标侧保留语义、父谓词是否拒 NULL、Probe 表达式是否 Propagate NULL、等值传播方向、Producer–Consumer 闭环。  
**适用：** 内核正确性审计、跨引擎对照、结果级回归设计。  
**不适用：** 不能用 Explain 里出现 RF、或下推层数，代替结果正确性。

一眼可用的边界：

| 场景 | 通常结论 |
|------|----------|
| Left/Full 对自己的保留侧当 Producer | 否（会删必须输出的未匹配行） |
| Right Join 用 Build Domain 滤非保留侧 | 可以（仍要完整 Build + Fail-Open） |
| 父 RF 经 Child Outer：先落保留侧 | 通常可提前删除最终也会被拒的行 |
| 父 RF 进入 NULL 生成侧 | 仅当 Probe 表达式 Null-Propagating，且父比较拒 NULL（或显式保留 nullAllowed 语义） |
| `coalesce` / `ifnull` 作 Probe | 不得进 NULL 生成侧 |
| 父条件为 `<=>` / `IS NOT DISTINCT FROM` | 不得按普通 `=` 做无向双边复制 |

`coalesce` 最小反例：

```sql
SELECT *
FROM A
LEFT JOIN B ON A.k = B.k
INNER JOIN C ON coalesce(B.k, 0) = C.k;
-- A.k=1, B.k=1, C.k=0 → 正确结果为空；
-- 若错误地把上层 DF 推到 B 并删掉 B.k=1，会合成 B=NULL，
-- coalesce(NULL,0)=0 通过上层，结果多一行。
```

![coalesce 反例：DF 进入 NULL 生成侧会改变匹配结果](/images/dynamic-filter-coalesce-counterexample.svg)

*图 1：左侧是不下推时的正确执行，右侧是把 C 侧 `{0}` 推到 B 后的结果。差别不在少读多少行，而在 `B.k=1` 被删后 LEFT JOIN 合成了一条 `B=NULL` 的行，`coalesce` 把它映射成可匹配值，使它穿过了原本拒绝 NULL 的上层谓词。*

下文钉问题边界与源码版本，再按 StarRocks / Doris / Trino 对照实现；**统一判据表与三系统对比表**仍是本文核心资产。

## 问题边界：生成过滤器与穿越 Outer Join

### 源码版本与结论范围

| 项目 | 源码快照 | 日期 | 重点入口 |
|------|----------|------|----------|
| StarRocks | [0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) | 2026-03-30 | `JoinNode`、`RuntimeFilterDescription` |
| Apache Doris | [5202d06](https://github.com/apache/doris/tree/5202d06dd8feb3390ff32839227eeee89c345b57) | 2026-08-21 | `RuntimeFilterGenerator`、`RuntimeFilterPushDownVisitor` |
| Trino | [68dae09](https://github.com/trinodb/trino/tree/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c) | 2026-08-22 | `PredicatePushDown`、`DynamicFiltersChecker` |

本文结论以以上本地源码快照为准。RF/DF 相关代码变化较快，尤其是 Outer Join 和 Null-Safe Equality 的支持范围，阅读其他版本时应重新核对 Rule、Session Variable 与回归测试。

2026-10 在更新快照上复核了三套实现（见下文「2026-10 复核记录」）。总体结论：本文描述的三种传播模型没有变，但 StarRocks 修掉了一个 TopN RF 的排序方向错误，Doris 新增了 RF 驱动的分区/分桶裁剪元数据。

#### 2026-10 复核记录

复核快照：StarRocks `f977b53`（2026-09-23）、Doris `7ac0eb2f`（2026-10-04）、Trino `6369330`（2026-10-06）。

**已核实未变**

- StarRocks `JoinNode.pushDownRuntimeFilters()` 的三级策略未变：仍是先 `pushDownRuntimeFilterBilaterally()`、失败后 `pushDownRuntimeFilterUnilaterally()`、最后在当前 Join 注册 Probe；`buildRuntimeFilters()` 的 Producer 限制、`checkRuntimeFilterOnNullValue()` 的 `equalForNull` 记录也仍在原位置。
- 本文标记的待验证边界仍然成立：`pushDownRuntimeFilterBilaterally()` 依旧只要求 Probe Slot 参与某个 `BinaryType.EQ`，没有检查父 RF 的 `equalForNull`。文中给出的 `IS NOT DISTINCT FROM` 形态仍需用结果级回归确认是否真的生成错误计划。
- Doris 的 `RuntimeFilterGenerator` 与 `RuntimeFilterPushDownVisitor` 主流程未变，`runtimeFilterPushDown` 的 Visitor 结构、生产者/消费者登记方式一致。
- Trino `createDynamicFilters()` 的准入条件仍是 `INNER || RIGHT` 加 session 开关；`nullAllowed` 仍随 `DynamicFilterExpression` 保留。两个快照之间 `PredicatePushDown.java` 的 192 行改动（+65/−127）是 `DynamicFiltersResult` 等内部类改为 record、getter 改为访问器，不改变传播语义。

**变化**

- StarRocks `RuntimeFilterDescription.isTopNSortAsc()` / `isTopNullsFirst()` 改为固定读取排序键下标 0。旧实现用 `exprOrder` 索引 `sortInfo`，而 `exprOrder` 表示 group-by 位置、不是排序列位置；多列 TopN 且分组位置与排序列不一致时，可能把降序当成升序构造 TopN RF。这是 RF 正确性问题，不影响 Outer Join 传播模型。
- StarRocks `canPushDownRuntimeFilterCrossExchange()` 增加 `DescriptorTable` 参数（跨 Exchange 下推时需要它做类型/表达式校验），调用点与语义不变。
- Doris 新增 `RuntimeFilterPruneClassifier`：`RuntimeFilterPushDownVisitor` 在 DF 落到 Scan 后调用 `RuntimeFilterContext.generateRuntimeFilterPruneMetadata()`，尝试为这条 RF 生成目标侧的分区裁剪与分桶裁剪元数据。分桶裁剪要求 RF 类型为 IN / IN_OR_BLOOM、目标是 `PhysicalOlapScan` 上的直接列、且该列是单列 HASH 分布键；分区裁剪要求 RANGE/LIST 分区、目标表达式落在单个分区列上、RANGE 分区不接受 BLOOM，表达式还要能判定单调。`enable_runtime_filter_bucket_prune` 与 `enable_runtime_filter_partition_prune` 两个开关默认开启。这属于“DF 到 Scan 之后还能做什么”的新能力，不改变本文的穿越判据。
- Trino 除上述重构外，未见 DF 方向推导逻辑变化；`processLimitedOuterJoin()` 仍要求父谓词先落到保留侧 Scope，再派生 NULL 生成侧副本。

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

文首工程判断已给出最小反例。要点：问题不是“NULL 会不会被滤掉”，而是**下推是否改变匹配状态，以及新补 NULL 行能否通过原谓词**。`slot` / 保 NULL 的 `cast` 通常可证；`coalesce` / `ifnull` / 未知 UDF 默认不可进 NULL 生成侧。

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

满足条件后两侧都会尝试（非短路）：同一 Filter ID 可落到 `Scan(A).k` 与 `Scan(B).k`。普通 `=` 拒 NULL 且无假阴性时能扩大过滤面；若父 RF 带 `equalForNull`，不能仅凭下层普通 EQ 无条件反向复制（见下节边界）。


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

过程要点：先把 inheritedPredicate 改写到保留侧 scope；成功后再沿 Join Equality 派生 NULL 生成侧副本，否则留在 Join 上方（`postJoinConjuncts`）。

```text
父层 DF → 必须先能写成保留侧谓词 → 保留侧留一份
        → 再派生 NULL 生成侧副本（只减匹配候选）
```

安全性依赖：保留侧仍持有原谓词；即便 Inner 侧匹配状态变化，不满足 DF 的保留行仍会被拒，不会凭空增多结果。


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

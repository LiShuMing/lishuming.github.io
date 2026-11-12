---
title: "【源码】Dynamic Filter 穿越 Outer Join：StarRocks、Doris 与 Trino 的正确性边界"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从一条仅含三行数据的 coalesce 反例出发，推导 Runtime Filter / Dynamic Filter 穿越 Outer Join 的数学安全边界，并深入对比 StarRocks、Apache Doris 与 Trino 的内核实现。"
draft: false
toc: true
math: true
---

> 本系列第 3/3 篇 · Join 执行 · [上一篇]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}}) · 相关：[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}})

在内连接（`INNER JOIN`）里，运行时过滤（Runtime Filter / Dynamic Filter，下文按语境简称 RF 或 DF）的原理直观得几乎不会出错：Hash Join 在构建（Build）端扫完小表后，顺手把连接键的值域压成紧凑摘要（`IN` 列表、`Min/Max` 区间或 `Bloom Filter`），逆流投递给探测（Probe）端的底层扫描算子。只要摘要集合 $D_{\text{filter}}$ 完整覆盖真实构建键集合 $D_{\text{build}}$（满足 $D_{\text{build}} \subseteq D_{\text{filter}}$，允许假阳性、绝无假阴性），被提前丢弃的探测行反正进不了最终连接结果，早扔晚扔在语义上完全等价。

顺着这个直觉往下走，最容易写出一条看似天经地义的推论：**既然连接条件代表等值传递，只要上下两层连接共享连接键，为什么不把上层连接构建的过滤器顺着等值链一路推到底层所有表上？**

一旦查询计划里混入外连接（`OUTER JOIN`），这条朴素推论第一步就会撞出致命的正确性事故。不妨在白板上推演一个只有三行数据的最小反例——表 `A` 有一行 `k = 1`，表 `B` 有一行 `k = 1`，表 `C` 有一行 `k = 0`：

```sql
SELECT *
FROM A
LEFT JOIN B ON A.k = B.k
INNER JOIN C ON coalesce(B.k, 0) = C.k;
```

我们先算不加任何运行时过滤器的标准答案：下层 `A LEFT JOIN B` 中，`A(1)` 与 `B(1)` 成功配对，吐出一行 `(A.k = 1, B.k = 1)`；送到上层与 `C(0)` 做内连接时，`coalesce(1, 0) = 1`，与 `C.k = 0` 不相等，整条 SQL 的正确输出是**空集（0 行）**。

现在，假设优化器看到上层内连接的右侧值域只有 `{0}`，大笔一挥把这个动态过滤器穿越 `LEFT JOIN` 推到了右表 `B` 的扫描端。灾难在瞬间发生：`B` 表原本那行 `B.k = 1` 不在 `{0}` 里，在进 `LEFT JOIN` 之前就被当场枪毙了！然而 `LEFT JOIN` 的左表 `A` 是保留侧（Preserved Side）——当右表配对行消失后，`A(1)` 并不会跟着消失，而是摇身一变，由外连接算子**凭空合成出一条右侧补空的新行 `(A.k = 1, B.k = NULL)`**。等到这行新合成的数据流到上层连接时，`coalesce(NULL, 0)` 把 `NULL` 映射成了 `0`，恰好撞上 `C.k = 0`，最终查询竟然吐出了 **1 行幽灵数据**！

![coalesce 反例：DF 进入 NULL 生成侧会改变匹配结果](/images/dynamic-filter-coalesce-counterexample.svg)

*图 1：左侧是不下推时的标准执行，右侧是将上层 `{0}` 过滤器推入补空侧 `B` 后的错行现场——提前删掉 `B.k = 1` 改变了外连接的匹配状态，迫使 `LEFT JOIN` 合成出原本不存在的 `B = NULL` 行，又被 `coalesce` 救活放行。*

看懂这三行数据，就能抓住动态过滤器穿越外连接的根本矛盾：**在内连接里，提前删掉一行输入只会“减少”中间结果；而在外连接的补空侧（Null-Supplying Side），提前删掉一行原本能配对的输入，会把“匹配态”篡改为“未匹配态”，从而“无中生有”地制造出补 `NULL` 的新行。**

要守住这条语义红线，查询引擎究竟需要证明哪些条件？StarRocks、Apache Doris 与 Trino 各自在源码里筑起了怎样的防线？下面我们从第一性原理出发逐一拆解。

---

## 一、拆解两个正交命题：谁能生成过滤器，谁能被过滤器穿越？

排查外连接与 Runtime Filter 的交互时，首要任务是把两个经常被混为一谈的问题彻底分开：

1. **生成准入（Producer Legality）**：当前这层 `OUTER JOIN` 自己能不能拿 Build 侧去过滤自己的 Probe 侧？
2. **穿越合法性（Passthrough Legality）**：**上层**某个连接产出的合法过滤器，能不能穿过**下层**这层 `OUTER JOIN` 落到底表扫描上？

先看第一个问题。假设单层查询就是 `SELECT * FROM A LEFT JOIN B ON A.k = B.k`，在主流引擎中通常以左表 `A` 为 Probe、右表 `B` 为 Build。此时左表 `A` 是保留侧，哪怕某行 `A.k` 在 `B.k` 的哈希表里完全找不到，这行 `A` 也必须填上 `B.* = NULL` 如数输出。如果拿 `B` 构建的过滤器去删 `A`，未匹配的保留行就会人间蒸发。因此，**`LEFT OUTER JOIN` 和 `FULL OUTER JOIN` 绝不能充当过滤自身保留侧的 Producer**；反过来，`RIGHT OUTER JOIN` 的右侧（Build）才是保留侧，左侧（Probe）是补空侧——未匹配的左表行本来就要被丢弃，只要完整收集完右表键值并在超时或内存超限时保持放行（Fail-Open），`RIGHT OUTER JOIN` 完全可以合法生成过滤左表的动态过滤器。

再看第二个、也是真正棘手的问题：当上层 `Parent Join` 产出了一个代表“最终结果必满足契约”的过滤器，往下遇到了子算子 `Child Left Join (A ⟕ B)`，它该往哪一侧走？我们按外连接两侧的角色分别推导：

| 外连接类型 | 保留侧（Preserved Side） | 补空侧（Null-Supplying Side） |
| --- | --- | --- |
| `LEFT OUTER JOIN` | 左子树 `A` | 右子树 `B` |
| `RIGHT OUTER JOIN` | 右子树 `B` | 左子树 `A` |
| `FULL OUTER JOIN` | 左右两侧皆保留 | 左右两侧皆可能补 `NULL` |

如果父层过滤器的探测表达式只引用**保留侧 `A`** 的列，把它推入 `A` 是天然安全的——因为无论某行 `A` 能不能在 `B` 里找到伴侣，它输出的 `A` 列值都原封不动；既然这行 `A` 到了父层连接注定被拒之门外，在进 `LEFT JOIN` 前提前剔除，只会省下无谓的哈希探测，绝不会多造或少造任何输出。

但如果父层过滤器想要踏入**补空侧 `B`**（无论是原本就引用 `B` 列，还是想顺着 `A.k = B.k` 把 `A` 上的过滤器复制给 `B`），就必须同时过完**双重 `NULL` 安全核验**：

- **第一重核验（父层比较算子本身拒绝 `NULL`，即 Null-Rejecting）**：普通等值 `=` 在一侧为 `NULL` 时求值为 `UNKNOWN`，天然拒绝空值；但如果是空值安全等值（`<=>` 或 `IS NOT DISTINCT FROM`），当父层 Build 端恰好含有 `NULL` 时，下层新合成的补空行就能合法配对！此时绝不能随意删减补空侧候选。
- **第二重核验（探测端标量表达式保持空值传递，即 Null-Propagating）**：探测表达式 $e(x)$ 必须满足 $e(\text{NULL}) = \text{NULL}$。裸列引用 `b.k`、保空类型转换 `CAST(b.k AS BIGINT)` 满足这一性质——一旦删掉 `B` 行触发补空，$e(\text{NULL})$ 吐出 `NULL`，撞上第一重核验中拒绝 `NULL` 的父层 `=`，新合成的补空行照样死路一条，语义严丝合缝；而 `coalesce(b.k, 0)`、`ifnull(b.k, 0)` 或行为未知的黑盒 UDF 会把 `NULL` 洗白成非空常数，必须严禁踏入补空侧！

把生产者完整性、目标侧角色、双重 `NULL` 证明与防死锁闭环串起来，就得到了动态过滤器穿越外连接的完整安全决策流：

![Dynamic Filter 穿越 Outer Join 的六步安全判据决策流](/images/diagrams/aug-opt-df-outer-join-boundary.svg)

*图 2：动态过滤器穿越外连接的六步安全判据——落向保留侧可直接放行；一旦试图进入补空侧，必须同时证明“父比较算子拒绝 `NULL`”且“探测表达式满足 $e(\text{NULL}) = \text{NULL}$”，否则必须截停在连接算子上方。*

下面这张速查表把最容易踩坑的六种工程场景定死在了明处：

| 典型计划形态 | 安全裁决与底层机理 |
| --- | --- |
| `LEFT / FULL JOIN` 拿右表 Build 过滤自身左表 | **禁止**：直接误删必须补 `NULL` 输出的保留侧未匹配行 |
| `RIGHT JOIN` 拿右表 Build 过滤自身左表 | **允许**：左侧本就是非保留侧，确保完整 Build 与异常 Fail-Open 即可 |
| 父层过滤器穿过子 `LEFT JOIN` 落向保留侧 `A` | **允许**：提前剔除最终在父层必死无疑的行，不改变任何存活行的配对状态 |
| 父层过滤器（普通 `=`）以裸列 `B.k` 落向补空侧 `B` | **允许**：即便删行诱发补 `NULL`，裸列传递 `NULL` 后仍被父层 `=` 拦截 |
| 父层以 `coalesce(B.k, 0) = C.k` 落向补空侧 `B` | **禁止**：破坏 $e(\text{NULL}) = \text{NULL}$，合成的补空行会被转成 `0` 假阳性放行 |
| 父层条件为 `B.k <=> C.k`（或 `IS NOT DISTINCT FROM`） | **禁止无向复制**：父层允许 `NULL` 配对，不可当作普通拒空等值穿透外连接 |

手握这把标尺，我们走进 StarRocks、Doris 和 Trino 的代码库，看看工业界三套主流引擎分别用怎样的架构表达这条边界。

---

## 二、StarRocks：双边等值优先的三级下推与 `equalForNull` 审视

StarRocks 的运行时过滤器在物理 `PlanNode` 构建阶段生成并自顶向下推送，核心逻辑集中在 [`JoinNode.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java) 与 [`RuntimeFilterDescription.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterDescription.java)。

在生成端，`JoinNode.buildRuntimeFilters()` 先卡住 Producer 准入门槛：

```java
if (!joinOp.isAnyInnerJoin()
        && !joinOp.isLeftSemiJoin()
        && !joinOp.isRightJoin()
        && !joinOp.isCrossJoin()) {
    return;
}
```

这里 `child(1)` 固定为 Build 端，`child(0)` 为 Probe 端：`LEFT OUTER`、`FULL OUTER` 与 `LEFT ANTI` 被直接挡在门外，而 `RIGHT JOIN` 合法入场过滤左侧非保留行。同时，每个生成的过滤器都会原原本本记下构建它的等值谓词是否属于空值安全比较（`<=>`）：

```java
rf.setEqualForNull(
        BinaryPredicate.IS_EQ_NULL_PREDICATE.apply(joinConjunct));
```

当上层生成的过滤器顺着计划树往下寻找消费落点、撞上下层另一个 `JoinNode` 时，`JoinNode.pushDownRuntimeFilters()` 摆出了极具进攻性的**“双边优先 → 单边回退 → 就地拦截”三级推进策略**：

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
// 若既无法双边也无法单边下穿，则注册在当前 JoinNode 输出端就地过滤
```

这套三级火箭的性能意图非常鲜明——只要安全条件凑齐，尽量让同一份 Filter ID 裂变覆盖到底层尽可能多的扫描表上：

1. **第一级 `pushDownRuntimeFilterBilaterally()`（双边等值复制）**：为什么 `coalesce` 反例没能打穿 StarRocks？秘密就在它的准入锁里——双边复制严苛要求当前连接不是 `Cross Join` 或 `Null-Aware Left Anti Join`，**传入的 `probeExpr` 必须是纯粹的裸列引用 `SlotRef`**（任何套了 `coalesce`、`ifnull` 甚至普通算术函数的表达式当场出局！），且该 `SlotRef` 直接参与了当前连接的普通等值条件 `BinaryType.EQ`。一旦放行，代码会以非短路方式同时向左右两个孩子下推同一份过滤器。
2. **第二级 `pushDownRuntimeFilterUnilaterally()`（单边保留方向回退）**：一旦 `probeExpr` 带了复合函数（如 `coalesce(A.k, 0)`）进不了双边通道，单边策略就会严格退守保留侧边界——遇到 `Left Outer` / `Left Anti` 只准往左孩子 `child(0)` 推，遇到 `Right Outer` / `Right Anti` 只准往右孩子 `child(1)` 推，从方向上彻底封死了复合表达式溜进补空侧的可能（跨 `Exchange` 节点时还会由 `canPushDownRuntimeFilterCrossExchange()` 结合 `DescriptorTable` 校验类型与分区映射一致性）。
3. **第三级就地消费与 `checkRuntimeFilterOnNullValue()`**：对于停留在连接附近或涉及合成空值的场景，代码会检查 `!description.getEqualForNull()`，把外连接合成后注定被拒空的列登记进 `filter_null_value_columns` 加速剔除。

用前面的统一标尺审视 `pushDownRuntimeFilterBilaterally()` 的局部代码，也能敏锐地抓出一个值得放进结果级回归测试重点盯防的组合边界：该函数核验了下层连接谓词必须是普通 `BinaryType.EQ`，但在函数体内并未显式复查**父层过滤器自身携带的 `description.getEqualForNull()`**。设想如下 SQL：

```sql
SELECT *
FROM (A LEFT JOIN B ON A.k = B.k)
INNER JOIN C ON B.k IS NOT DISTINCT FROM C.k;
```

当 `C.k` 包含 `NULL` 时，上层连接产出的过滤器带有 `equalForNull = true`（允许 `NULL` 通过）；左表 `A` 中未匹配的行原本会在补出 `B.k = NULL` 后与 `C.NULL` 成功牵手。倘若此时仅凭下层 `A.k = B.k` 把 `B.k` 上的过滤器反向复制给左表 `A.k`，而 `A.k` 本身是非空值（比如 `A.k = 99`，不在 `C.k` 非空集合里），提前在 `Scan(A)` 处杀掉 `A.k = 99` 就会误伤那条本该靠补 `B.k = NULL` 命中的结果行！尽管上游改写规则与候选槽位推导可能在其他环节拦住这一形态，在内核单测中把“父层 `IS NOT DISTINCT FROM` + 子层 `LEFT JOIN`”列为必测反例，正是防御跨层语义漏网的最佳实践。

---

## 三、Apache Doris：血缘直穿与等值扩展的显式解耦

如果说 StarRocks 是“先试双边等值复制、不成再退单边”，Apache Doris 的 Nereids 优化器则在物理后处理阶段（[`RuntimeFilterGenerator`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java) 与 [`RuntimeFilterPushDownVisitor`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterPushDownVisitor.java)）做了一次极其干净的正交拆解：**把“顺着原表达式的列血缘往下走”和“借用连接等值条件跨侧派生新目标”拆成了两条完全独立的证明路径。**

首先在 Producer 端，`RuntimeFilterGenerator` 用一份明确的黑名单拒绝为 `LEFT_OUTER_JOIN`、`FULL_OUTER_JOIN`、`LEFT_ANTI_JOIN`、`NULL_AWARE_LEFT_ANTI_JOIN` 及 `ASOF_LEFT_OUTER_JOIN` 生成过滤自身保留侧的 RF；紧接着在 `visitPhysicalHashJoin()` 中，如果发现产生产生该过滤器的父连接使用的是空值安全等值 `NullSafeEqual`（`<=>`），而当前下穿途中撞上了任何 `Outer Join`，当场返回 `false` 刹停，直接从根源上掐灭了上一节讨论的空值安全跨层传染隐患：

```java
if (equal instanceof NullSafeEqual
        && join.getJoinType().isOuterJoin()) {
    return false;
}
```

排除了 `<=>` 之后，Doris 分两条路推进：

- **路径一（沿原表达式血缘直穿，`canPushThroughJoinChild`）**：检查当前 `probeExpr` 引用的列归属哪一侧孩子。如果那个孩子不是当前连接的补空侧（比如 `LEFT JOIN` 的左孩子），直接放行；如果那个孩子**正是补空侧**（比如 `LEFT JOIN` 的右孩子），则调用 `isNullPropagating(ctx.probeExpr)` 验明正身——只有裸 `Slot`、保空的 `Cast` 以及所有子参数递归满足保空性的 `PropagateNullable` 函数才能拿到通行证！像 `coalesce(c_custkey, 0)` 这种吞掉 `NULL` 吐常数的函数当场被拒之门外。Doris 仓库不仅在 [`RuntimeFilterTest.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/test/java/org/apache/doris/nereids/postprocess/RuntimeFilterTest.java) 里锁死了计划形状断言，还在回归套件 [`test_runtime_filter_outer_join_nullable_side.groovy`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/regression-test/suites/correctness_p0/test_runtime_filter_outer_join_nullable_side.groovy) 中用真实表数据验证了 `coalesce` 反例的结果行数。
- **路径二（跨侧等值派生，Equality Expansion）**：当想借用下层连接的 `leftExpr = rightExpr` 把一侧的过滤器克隆给另一侧时，Doris 立下了铁规——除了要求开启 `expand_runtime_filter_by_inner_join` 且新目标收敛于单一 `Slot` 外，**当前被借道的连接本身只准是 `Inner Join` 或 `Semi Join`！** 换句话说，Doris 允许保空表达式顺着自家血缘走入外连接的补空侧，但绝不允许把外连接的等值条件当成无向跳板去跨侧复制过滤器。

一旦过滤器顺利抵达底表 `visitPhysicalRelation()`，Doris 还会检查目标扫描节点能力、拒绝在非数值列上执行昂贵的函数计算，并在新近主干中通过 `RuntimeFilterPruneClassifier`（`generateRuntimeFilterPruneMetadata()`）进一步挖掘出分区裁剪（`RANGE/LIST` 分区上的单调表达式且非 Bloom）与分桶裁剪（单列 `HASH` 分桶键上的 `IN` 过滤器）元数据，让穿越下来的动态过滤器不仅能少读页，还能直接整片砍掉底层的 Tablet 扫描分片。

---

## 四、Trino：保留侧锚定的有向作用域推导与防死锁校验

Trino 没有单独为动态过滤器发明一套物理树游走器，而是把 `DynamicFilterExpression` 当作一种携带特殊标签的谓词，无缝融进了经典的谓词下推引擎 [`PredicatePushDown.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/optimizations/PredicatePushDown.java) 中，由此展现出第三种极具代数美感的工程解法。

在 `createDynamicFilters()` 入口，Trino 同样只允许 `INNER` 与 `RIGHT` 连接创建动态过滤器，并在遇到 `IS NOT DISTINCT FROM` 时显式给表达式打上 `nullAllowed = true` 印记，确保空值许可契约伴随表达式走完全程。

当父层传下来的动态过滤器作为 `inheritedPredicate` 撞上下层 `LEFT JOIN` 时，接管大权的是 `processLimitedOuterJoin()`。它的核心哲学可以用一句话概括：**“想进补空侧？先证明你能写成保留侧的谓词！”**

1. 优化器首先利用等值推理器（`EqualityInference`），尝试把父层传下来的 `inheritedPredicate` 纯粹用**保留侧（Outer Scope，即左表）**的符号表达出来（记作 `outerRewritten`），并稳稳压在保留侧头上；
2. 只有当这一步改写成功时，才允许顺着连接等值关系，再往**补空侧（Inner Scope，即右表）**派生出一份镜像过滤副本；
3. 如果父层动态过滤器直接引用的是右表 `B` 的列（比如 `B.k = C.k` 或 `coalesce(B.k, 0) = C.k`），由于单靠左表到右表的外连接无法反向把右表谓词等价改写到左表保留域上，`outerRewritten` 求值失败，整条动态过滤器就会被乖乖留在外连接上方的 `postJoinConjuncts` 里，一步也不准往下迈！

细品一下这套“保留侧锚定法”为什么稳如泰山：既然保留侧 `A` 已经死死摁住了原谓词的等价副本，任何不满足父层过滤条件的左表行在扫描 `A` 时就已被干掉；至于幸存下来的左表行，右表 `B` 的镜像副本只会帮它提前踢掉那些“配不上对”的右表行，绝不可能让原本不该存活的左表行蒙混过关！这种设计牺牲了“父层直接查 `B.k = C.k` 时单穿右表 `B`”的激进收益，换来了无需逐个审计复杂函数保空性的极简代数安全性。

不仅如此，Trino 还在规划收尾处部署了两道严密的卫兵：[`RemoveUnsupportedDynamicFilters`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/RemoveUnsupportedDynamicFilters.java) 负责清理没能落到合法 `TableScan` 上的孤儿过滤器；而语法健康检查器 [`DynamicFiltersChecker`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/sanity/DynamicFiltersChecker.java) 则逐条核验每对 Producer–Consumer 闭环，尤其严禁**同一个 Join 的 Build 子树反向消费自己构建的 DF**——否则执行期就会陷入“Build 等 Scan 吐数据才能建哈希表、Scan 又傻等 Build 建完发 DF 才肯读盘”的分布式死锁。

---

## 五、三套引擎横向对照与运行期预算治理

把 StarRocks、Doris 和 Trino 的外连接穿越路径摆在同一张架构图上，能够清晰看到三种截然不同却殊途同归的工程取舍：

![StarRocks、Doris 与 Trino 穿越 Outer Join 的三套传播机制对照](/images/diagrams/aug-opt-df-three-engines.svg)

*图 3：三套引擎穿越外连接的传播机制对照——StarRocks 以裸列 `SlotRef` + 普通 `EQ` 优先铺开双边过滤面，Doris 将沿血缘保空直穿与 `Inner/Semi` 等值扩展严格分家，Trino 则以保留侧作用域改写为前置锚点并辅以防死锁校验。*

我们将各维度的设计细节浓缩为下表，方便跨引擎对齐排障：

| 对比维度 | StarRocks | Apache Doris (Nereids) | Trino |
| --- | --- | --- | --- |
| **规划介入时机** | 物理 `PlanNode` 构建后自顶向下推送 | `PlanPostProcessors` 独立后处理遍历 | 迭代规则 `PredicatePushDown` 统一代数推导 |
| **Producer 准入** | `Inner` / `Left Semi` / `Right` / `Cross` | 显式拉黑 `Left` / `Full` / `Left Anti` / `ASOF` 等 | 仅限 `INNER` 与 `RIGHT` 连接 |
| **外连接穿越主策略** | `Bilateral` 双边优先 → `Unilateral` 保守单侧 → 就地注册 | 原血缘直穿与跨侧等值扩展（Expansion）彻底分离 | 先锚定改写至保留侧 Scope，成功后再向补空侧派生 |
| **跨侧等值复制范围** | 裸 `SlotRef` 参与下层普通 `EQ` 时允许双边尝试 | **仅限 `Inner` / `Semi` 连接**，禁止借外连接跨侧扩展 | 外连接允许从保留侧单向派生至补空侧，禁止反向 |
| **表达式保空门禁** | 双边路径硬限制为裸列 `SlotRef` | 递归校验 `Slot` / 保空 `Cast` / `PropagateNullable` | 靠保留侧作用域改写天然隔离补空侧复杂函数 |
| **Null-Safe (`<=>`) 防护** | 携带 `equalForNull` 标记并控制 `filter_null_value_columns` | 父连接为 `NullSafeEqual` 时遇任何外连接立即停止下推 | 携带 `nullAllowed` 属性参与作用域与类型一致性校验 |
| **收尾与防环保障** | 跨 `Exchange` 校验 + 落点探测 | `RuntimeFilterPruner` 剪除无效项（保留分区/分桶裁剪项） | `RemoveUnsupportedDynamicFilters` + `DynamicFiltersChecker` |

语义正确只是动态过滤器的及格线；上了百节点生产集群，还必须管住**值域膨胀（Domain Bloating）的资源账单**。如果右表 Build 端有上千万个互不相同的键，强行序列化精确的 `IN` 集合会把协调节点内存和网络带宽双双打爆；哪怕退化为 `Bloom Filter`，一旦过滤率接近 0%，全集群广播加上扫描端逐行算哈希也是纯亏本买卖。

因此成熟的 Producer–Consumer 运行时协议都维护着一套可降级的状态机：从初始收集态 `COLLECTING` 出发，在基数与字节阈值内产出完整的精确集合或 `Bloom`（`COMPLETE`）；一旦唯一值基数超标则主动降级为轻量的 `MinMax` 甚至直接放弃（`TOO_LARGE`，详见 [Trino Dynamic Filtering 阈值机制](https://trino.io/docs/current/admin/dynamic-filtering.html)）；遇到网络拥塞超时或查询取消则无条件触发 `Fail-Open` 放行，绝不让一条优化旁路拖垮主查询的可用性。阅读线上 Query Profile 时，也一定要把 `Build Rows / NDV`、`Serialized Bytes`、`Merge / Wait Time` 与扫描端真正跳过的 `Pages / Splits` 对照起来看，揪出那些“广播跑遍全网、一行没滤掉”的无效过滤器。

---

## 六、统一核验清单、回归测试矩阵与源码索引

如果你正在参与优化器内核研发，或者准备给自己的查询引擎补齐外连接上的动态过滤下推，可以把整套证明浓缩成五个接口职责与六步核验门禁：

1. **`canGenerate(joinType, eqPred)`**：核验当前连接是否非保留侧探测（拒绝 `Left/Full/Anti` 滤除保留行），记录构建端完整性契约与空值比较标志（`equalForNull` / `nullAllowed`）；
2. **`canPassThrough(joinType, targetSide, probeExpr, filterMeta)`**：沿既有列血缘下穿时，若目标是保留侧则放行；若目标是补空侧，必须同时断言“父比较拒绝 `NULL`”且“`probeExpr` 满足空值传递 $e(\text{NULL}) = \text{NULL}$”；
3. **`rewriteAcrossJoin(joinType, joinEq, srcExpr, dstSide)`**：跨连接键向对侧派生新过滤器时，严禁把外连接的等值条件当作无向等价类双向乱抄（要么像 Doris 那样仅限 `Inner/Semi`，要么像 Trino 那样先锚定保留侧再单向派生补空侧）；
4. **`canConsume(scanNode, targetExpr, rfType)`**：评估底表连接器支持度、分区分桶裁剪潜力以及函数求值开销；
5. **`validateGraph(producers, consumers)`**：剪除没有任何消费者落点的孤儿 Producer，并静态断言不存在“Build 子树消费自身产出过滤器”的环形依赖。

在编写回归测试时，千万不要只用 `EXPLAIN` 文本里出没出现 `runtime_filter` 来判对错——没触发下推时结果本来就是对的，错误下推偏偏在均匀随机测试集上又容易侥幸逃过一劫。一套兜得住底的结果级回归矩阵，必须用包含 **`NULL` 值、空 Build 表、重复键以及“删掉原配对行后会诱发补 `NULL` 行”** 的精准数据，跑通以下高危 SQL 组合：

```sql
-- 1. 补空侧套非保空函数（核心红线：防删除原配对行后合成 NULL 被洗白）
SELECT * FROM A LEFT JOIN B ON A.k = B.k
INNER JOIN C ON coalesce(B.k, 0) = C.k;

-- 2. 父层为空值安全等值（防携带允许 NULL 语义的过滤器无向穿透外连接）
SELECT * FROM (A LEFT JOIN B ON A.k = B.k)
INNER JOIN C ON B.k IS NOT DISTINCT FROM C.k;

-- 3. 全外连接双侧补空（两侧均兼具保留侧与补空侧双重身份）
SELECT * FROM (A FULL OUTER JOIN B ON A.k = B.k)
INNER JOIN C ON A.k = C.k;

-- 4. Left Anti / Null-Aware Anti（NOT EXISTS / NOT IN 下删除候选行直接反转真值）
SELECT * FROM A WHERE A.k NOT IN (SELECT B.k FROM B);
```

最后附上三套引擎核心实现源码的阅读路标：

| 核心模块 | 所属引擎 | 关键源码路径 |
| --- | --- | --- |
| RF 生成与双边/单边三级下推 | StarRocks | [`JoinNode.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/JoinNode.java)、[`RuntimeFilterDescription.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterDescription.java) |
| Nereids RF 生成与血缘/扩展分离下推 | Apache Doris | [`RuntimeFilterGenerator.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterGenerator.java)、[`RuntimeFilterPushDownVisitor.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/main/java/org/apache/doris/nereids/processor/post/RuntimeFilterPushDownVisitor.java) |
| 补空侧 `coalesce` 单测与结果级回归 | Apache Doris | [`RuntimeFilterTest.java`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/fe/fe-core/src/test/java/org/apache/doris/nereids/postprocess/RuntimeFilterTest.java)、[`test_runtime_filter_outer_join_nullable_side.groovy`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/regression-test/suites/correctness_p0/test_runtime_filter_outer_join_nullable_side.groovy) |
| 有向作用域推导与完整性防环校验 | Trino | [`PredicatePushDown.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/optimizations/PredicatePushDown.java)、[`RemoveUnsupportedDynamicFilters.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/iterative/rule/RemoveUnsupportedDynamicFilters.java)、[`DynamicFiltersChecker.java`](https://github.com/trinodb/trino/blob/68dae096719f5ac7a14d5a5cbcbfa1b247c0620c/core/trino-main/src/main/java/io/trino/sql/planner/sanity/DynamicFiltersChecker.java) |

---

> **源码基线说明**：本文重构自前期内部技术调研笔记[《Dynamic Filter 下推能力调研》](https://alidocs.dingtalk.com/i/nodes/YMyQA2dXW7gYo6Mzc147ebrwWzlwrZgb)，初稿源码快照基于 `StarRocks/starrocks` 提交 `0fd27fd`（2026-03-30）、`apache/doris` 提交 `5202d06`（2026-08-21）及 `trinodb/trino` 提交 `68dae09`（2026-08-22），并于 2026-10 对照上游快照 StarRocks `f977b53`、Doris `7ac0eb2f`、Trino `6369330` 完成全量复核。复核期间三套引擎的外连接穿越模型保持稳定；上游演进细节（StarRocks 修正 `RuntimeFilterDescription.isTopNSortAsc()` 多列 TopN 排序键下标 `0` 读取并为跨 Exchange 下推补充 `DescriptorTable` 校验、Doris 新增 `RuntimeFilterPruneClassifier` 支撑动态分区/分桶裁剪、Trino 将 `DynamicFiltersResult` 重构为 Java `record`）均已同步校准融入正文。

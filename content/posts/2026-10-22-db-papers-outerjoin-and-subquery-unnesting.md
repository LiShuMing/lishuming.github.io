---
title: "【Paper】外连接重排与子查询去相关演进：从 COUNT 空集陷阱、EELs 到 HyPer 任意嵌套解耦"
date: 2026-10-22T12:20:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "db-papers-outerjoin-and-subquery-unnesting"
description: "从外连接非结合律反例与 COUNT(*)=0 关联子查询陷阱出发，系统拆解 Rosenthal 带标签空值代数、IBM EEL 静态冲突与补偿、Dayal 广义外连接聚合、R* 分布式求值协议、Kiessling 空域复审，直至 HyPer 依赖连接任意去相关与全谱系连接算子。"
draft: false
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "查询优化器", "Join Reorder", "Subquery Unnesting"]
series: ["数据库经典论文精读"]
---

> 本系列第 3/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：查询优化器演进]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}) · [下一篇：数据摘要与概率计数]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}})

纯内连接（Inner Join）构成的查询世界之所以令优化器心旷神怡，是因为关系乘法满足完美的交换律与结合律：无论把 `(R ⋈ S) ⋈ T` 改写成 `R ⋈ (S ⋈ T)` 还是 `(R ⋈ T) ⋈ S`，最终产出的元组集合分毫不差，动态规划枚举器只需放心大胆地挑选代价最低的连接树。然而真实生产环境里的 SQL 远不止等值内连接——业务报表只要想保留“尚未成交的客户”就会写出 `LEFT OUTER JOIN`，只要想排查“没有任何异常记录的账户”就会写出 `NOT EXISTS` 或 `NOT IN`，只要想按每行上下文动态比对阈值就会写出关联子查询（Correlated Subquery）。一旦跨入这片水域，内连接优化器赖以生存的两块基石——**算子结合律**与**空集过滤单调性**——会同时崩塌。

在展开七篇奠基论文之前，我们先在白板上推演两个只有两行数据的极简反例，亲眼看看朴素优化规则是如何在毫秒之间悄悄篡改查询结果的。

## 从两行反例看关系代数的两道裂痕：非结合律与空集消失

### 裂痕一：两行数据击穿外连接与内连接的结合律

假设三张极小表：`R(a) = {(1), (2)}`，`S(a, b) = {(1, 10), (2, 20)}`，而 `T(b) = {(10)}` 只包含一行。考虑如下查询：

```sql
SELECT R.a, S.b, T.b
FROM R
LEFT JOIN (S INNER JOIN T ON S.b = T.b)
  ON R.a = S.a;
```

按照括号规定的语义顺序，我们先计算右侧括号内的 `S ⋈ T`，只留下 `(a=1, b=10)` 这一行；随后用 `R` 左外连接该结果，`R` 中的 `(1)` 成功配对产出 `(1, 10, 10)`，而 `R` 中的 `(2)` 在右侧找不到伙伴，触发外连接的保留（Preserving）机制补上空值，产出 `(2, NULL, NULL)`。最终正确答案是 **2 行**：`{(1, 10, 10), (2, NULL, NULL)}`。

假如查询优化器看到 `S` 很大而 `R` 很小，像对待内连接一样套用结合律，把计划重排为 `(R LEFT JOIN S ON R.a = S.a) INNER JOIN T ON S.b = T.b`，会发生什么？第一步 `R ⟕ S` 产出 `{(1, 10), (2, 20)}`；紧接着第二步与 `T = {(10)}` 做内连接时，谓词 `S.b = T.b` 是空值拒绝（Null-Intolerant）且值敏感的，`20 = 10` 为假，`(2, 20)` 被当场滤除！最终结果只剩下 **1 行** `{(1, 10, 10)}`，原本受 `LEFT JOIN` 保护的左表元组 `R(2)` 凭空蒸发。哪怕将上例中的 `INNER JOIN` 换成反半连接（Antijoin，对应 `NOT EXISTS`），令 `T = ∅`、`S = ∅`，`R ⟕ (S ▷ T)` 会补空保留 `R` 的两行，而 `(R ⟕ S) ▷ T` 虽在空表上碰巧相同，一旦令 `R = {(1)}`、`S = {(1, 10)}`、`T = {(10)}`，前者因 `S ▷ T = ∅` 产出 `(1, NULL)`，后者却因 `(1, 10)` 命中 `T(10)` 被反连接彻底抹去，输出 **0 行**。

### 裂痕二：COUNT(*) = 0 关联子查询陷阱与补空计数错觉

再看第二道裂痕。设有部门表 `DEPT(id, name, budget) = {(10, 'Sales', 500), (20, 'Lab', 0)}` 和员工表 `EMP(emp_id, dept_id) = {(101, 10)}`——其中 20 号部门刚刚成立，预算为 `0`，名下暂时没有一名员工。业务想找出“预算不超过员工人数乘以 1000”的部门：

```sql
SELECT D.id, D.name
FROM DEPT D
WHERE D.budget <= 1000 * (
    SELECT COUNT(*)
    FROM EMP E
    WHERE E.dept_id = D.id
);
```

按照 SQL 的逐元组替换（Tuple-at-a-Time Substitution / Nested Iteration）参考语义：
- 代入 `D = (10, 'Sales', 500)`：内层匹配到员工 `101`，`COUNT(*) = 1`，判断 `500 <= 1000` 成立，保留 `10`。
- 代入 `D = (20, 'Lab', 0)`：内层过滤结果为空集 `∅`，但**标量 `COUNT` 在空输入上的数学定义是整数 `0`（而非 `NULL`）**！判断 `0 <= 1000 * 0` 即 `0 <= 0` 成立，保留 `20`。因此正确答案是 `{10, 20}`。

然而逐行扫描内表的时间复杂度高达 \(O(|D| \cdot |E|)\)。为了消除嵌套循环，Won Kim 在 1982 年经典论文中提出著名的 `NEST-JA` 规则：先对内表 `EMP` 按关联键预分组计算聚合临时表 `TEMP(dept_id, cnt) = SELECT dept_id, COUNT(*) FROM EMP GROUP BY dept_id`，再把外层查询改写为 `DEPT` 与 `TEMP` 的等值内连接 `D.id = TEMP.dept_id AND D.budget <= 1000 * TEMP.cnt`。把这个改写放到上面的两行数据上一跑，灾难立即浮现：由于 `EMP` 里根本没有 `dept_id = 20` 的记录，`GROUP BY` 只产出 `{(10, 1)}`——**“空集标量计数等于 `0`”的状态，在分组算子里退化成了“结果集中根本不存在这一行”**！随后的内连接找不到 `20` 的匹配行，直接把 `Lab` 部门悄悄丢弃，只返回错误答案 `{10}`。这就是数据库优化器史上最臭名昭著的 **COUNT Bug**。

更微妙的是，假如有人灵机一动：“既然内连接会丢掉空组，那我把 `DEPT` 和 `EMP` 改成左外连接 `DEPT LEFT JOIN EMP ON D.id = E.dept_id`，再按 `D.id` 做 `GROUP BY` 求 `COUNT(*)` 不就行了？”再算一遍 `20` 号部门：左外连接为了保留 `20`，会拼出一条右侧补空的占位元组 `(20, 'Lab', 0, NULL, NULL)`；此时分组里明明有 1 条物理元组，`COUNT(*)` 会毫不犹豫地算出 **`1`** 而不是 **`0`**！倘若把原查询条件改成 `D.budget < 1000 * (SELECT COUNT(*) ...)`，`0 < 1000 * 1` 就会把本该淘汰的零员工部门误判为有 1 名员工而放行。

这两道裂痕揭示了同一个第一性原理矛盾：**外连接、反连接与关联子查询的本质，都是在关系乘积之上引入了“依赖于某个局部作用域是否为空集”的非单调分支语义**。想要在不改变一丝一毫语义（包括 `NULL` 三值逻辑、多重集重复行数以及标量多行报错）的前提下解锁全局连接重排与哈希流水线，数据库界整整攻坚了三十余年。本篇精读的 7 篇经典论文，完整串联起了从 1984 年带标签空值代数、Dayal 广义外连接聚合、IBM DB2 的 EEL 冲突列表，一直到慕尼黑工业大学（TUM）HyPer 引擎实现任意嵌套 SQL 彻底去相关的演进脉络。

---

## 外连接与反连接重排：从带标签空值代数到 EEL 静态冲突规则

既然外连接破坏了普通连接树的结合律，优化器面临的第一个架构抉择便是：**如何在不推翻既有连接顺序搜索框架的前提下，精确刻画哪些重排合法、哪些重排需要事后修补？** 下图展示了从 Rosenthal 带标签空值代数到 IBM EEL 静态资格门控与广义外连接补偿的双通道机制：

![外连接与反连接重排机制：非结合律冲突、EEL 静态资格与补偿清理](/images/diagrams/paper-unnest-outerjoin-eels.svg)

### Rosenthal & Reiner (VLDB 1984)：用带标签空值与支配算子拆解外连接 {#paper-04}

- **论文来源**：Arnon Rosenthal, David Reiner, *Extending the Algebraic Framework of Query Processing to Handle Outerjoins*, VLDB 1984 · [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/extending-the-algebraic-framework-of-query-processing-to-handle-outerjoins.pdf)（核心重写树示例见 `Figure 5–8`，PDF p. 8–10；算子代数定义见 PDF p. 4–6）。

1980 年代初的查询优化器围绕“展平（Flattening）笛卡尔积 -> 枚举连接结合次序 -> 反展平（Unflattening）下推选择与投影”三步曲构建。Rosenthal 与 Reiner 敏锐地意识到，如果把外连接当成一个不可拆解的黑盒二元算子，每一次视图展开（View Substitution）或连接交换都要重写整套优化器搜索引擎：

> "By expressing queries and processing strategies in terms of relational algebra, one can use the ordinary mechanisms of query optimization and view substitution with a minimum of disruption." *(PDF p. 1)*

为了把外连接拆回普通笛卡尔积（`×`）加上可移动的一元算子，论文引入了一套精巧的**带标签空值（Labelled Nulls）与支配剔除（Dominance Omit）代数**：

1. **增广算子 `Aug<R|#S>` 与来源标签 `#R`**：每个可能作为非保留侧（补空侧）的关系或视图 \(R\) 分配一个唯一空值标签 `#R`。一元算子 \(\text{Aug}\langle R \mid \#S \rangle = R \cup \{(\#S, \dots, \#S)\}\) 向输入关系人为注入一条全为 `#S` 的“哨兵空元组”（初始查询树中满足 \(S = R\)，视图展开下推后才出现跨关系标签，要求 \(\text{attrib}(R) \subseteq \text{attrib}(S)\)）。
2. **放宽选择 `Sel(P/#S)`**：普通选择谓词遇空即假，会把哨兵元组直接杀掉。放宽谓词 \((P / \#S)\) 定义为：“\(P\) 为真，或者 \(S\) 中所有出现在 \(P\) 内的属性在当前元组上均取值为 `#S`”。这样一来，\(R_1 \times \text{Aug}\langle R_2 \mid \#R_2 \rangle\) 经过 \(\text{Sel}(P / \#R_2)\) 后，不仅保留了所有真实匹配行，还让每一个 \(R_1\) 元组都保底拥有一条与 \((\#R_2, \dots, \#R_2)\) 拼成的候选补空行！
3. **支配偏序与 `Omit<R|#S>`**：上述放宽连接多造出了大量“明明有真实匹配，却还跟着一条补空影子”的伪元组。作者定义标量偏序：\(x\) 在 `#S` 意义上支配（`#S-dominates`）\(y\)，当且仅当 \(x = y\)，或 \(x\) 非空且 \(y = \#S\)；不同元组 \(t'\) 逐属性支配 \(t\) 时称 \(t'\) 支配 \(t\)。紧接着用 `Omit` 算子扫除所有存在支配者的弱势元组：

> "A join that uses a relaxed predicate yields tuples not present in the outerjoin. The Omit operator below throws away dominated tuples." *(PDF p. 4)*

用这套积木，单侧左外连接 \(R_1 \mathbin{\text{⟕}}_P R_2\) 被严格还原为一串一元算子包裹的普通笛卡尔积：

$$\text{Proj}\Big[\text{Omit}\big\langle \text{Sel}(P / \#R_2)\big(R_1 \times \text{Aug}\langle R_2 \mid \#R_2 \rangle\big) \;\big|\; \#R_2 \big\rangle,\; \text{output-list}\Big]$$

拿一组两行实例把这串算子在黑板上跑一遍，立刻就能看懂为什么它能避开定制二元算子。设 \(R_1(a) = \{(1), (2)\}\)，\(R_2(b) = \{(1)\}\)，外连接谓词 \(P\) 为 \(a = b\)：

1. **注入哨兵**：\(\text{Aug}\langle R_2 \mid \#R_2 \rangle = \{(1),\, (\#R_2)\}\)；
2. **普通笛卡尔积**：\(R_1 \times \text{Aug}\langle R_2 \mid \#R_2 \rangle\) 生成 4 条组合：`{(1, 1), (1, #R2), (2, 1), (2, #R2)}`；
3. **放宽选择 `Sel(a = b / #R2)`**：`(2, 1)` 因非空且不等被滤除，但两条带 `#R2` 的候选行与真实匹配 `(1, 1)` 一同过关，留下 **3 行**：`{(1, 1), (1, #R2), (2, #R2)}`；
4. **支配剔除 `Omit< · | #R2>`**：比较同一左键的两条元组——`(1, 1)` 在第 1 列与 `(1, #R2)` 相等，在第 2 列以非空值 `1` 支配 `#R2`，因此伪行 `(1, #R2)` 被清除；而 `(2, #R2)` 没有任何支配者，安全存活为最终补空结果 `{(1, 1), (2, #R2)}`！

代数的威力在多层外连接视图展开（Flattening，PDF p. 5）中立刻显现。假设视图 \(V = R_1 \mathbin{\text{⟕}} R_2\) 被外层 \(T \mathbin{\text{⟕}} V\) 引用，外层增广 \(\text{Aug}\langle V \mid \#V \rangle\) 需要向下穿过 \(V\) 内部的算子直达叶节点。当 \(\text{Aug}\langle R_1 \times R_2 \mid \#V \rangle\) 穿过笛卡尔积时，若简单拆成 \(\text{Aug}\langle R_1 \mid \#V \rangle \times \text{Aug}\langle R_2 \mid \#V \rangle\)，就会凭空拼出“左边真实 \(R_1\) 行、右边 `#V` 补空行”的非法混血元组。为此论文引入**一致性选择 `Unif(#S)`**，强制约束同一标签辖域内的属性要么全部是 `#S`，要么全部不是 `#S`。

而在选择完连接结合次序后的反展平下推阶段（Unflattening，PDF p. 6），算子能否穿过 `Omit` 取决于两组严格区分的数学条件：
- **`#S`-单调性（`#S-monotonicity`，Rule 4）**：若元组 \(t \in \text{op}(R)\) 且 \(t'\) 支配 \(t\)，把输入中的 \(t\) 换成更具体的 \(t'\) 后仍有 \(t' \in \text{op}((R - \{t\}) \cup \{t'\})\)，即算子“偏爱非空真实值胜过 `#S`”（例如不引用 `#S` 的常规比较谓词），此时 \(\text{Omit}\langle \text{op}(R) \mid \#S \rangle = \text{op}(\text{Omit}\langle R \mid \#S \rangle)\)。反之，形如 `IS #S`（检测未匹配补空行，用于把全称量词改写为外连接加 `Has_null(#R2)`）的谓词偏爱空值、拒绝真实值，绝不满足 `#S`-单调性，强行与 `Omit` 交换就会算出全空结果。
- **空值拒绝简化（Null Rejection，Section 3.8）**：若位于 `Aug<R|#S>` 上方的普通选择谓词 `Sel(P)` 对全 `#S` 补空元组恒为假（如原文 PDF p. 6 的 `FRACTION = 0.5` 排除了 `STAFF` 补空行），则 `Aug` 与对应的 `Omit` 可整套消去，外连接自动退化为普通内连接。这也从代数根源解释了现代 SQL 中为什么 `WHERE` 与 `ON` 的一字之差会产生截然不同的计划：

```sql
-- 写法 A：过滤写在 ON 内 -> 对应放宽选择 Sel((R1.a = R2.a AND R2.x = 5) / #R2)
-- 补空哨兵 (#R2, #R2) 自动豁免通过，未匹配 R1 行保留，绝对不能转内连接！
SELECT * FROM R1 LEFT JOIN R2 ON R1.a = R2.a AND R2.x = 5;

-- 写法 B：过滤写在顶层 WHERE -> 对应紧跟在 Omit 之后的严格选择 Sel(R2.x = 5)
-- (#R2 = 5) 恒为假，触发 Null Rejection，Aug 与 Omit 整套消去，安全退化为 R1 ⋈ R2！
SELECT * FROM R1 LEFT JOIN R2 ON R1.a = R2.a WHERE R2.x = 5;
```

阅读原文时还需留意两处历史边界：其一，整套代数建立在集合语义（Set Semantics）上，作者在 PDF p. 6 讨论“删除无输出列且仅参与外连接的非保留侧关系”时专门警告，若保留侧一行匹配非保留侧多行，删表会改变返回重复元组的数量（Bag 重数）；其二，PDF p. 4 印刷的双侧全外连接公式在乘积输入处只印出了 `R1 × Aug<R2|#R2>` 而漏印了对 `R1` 的 `Aug<R1|#R1>`，对照同页文字定义即可识别这一排版疏漏。

### Rao, Lindsay, Lohman, Pirahesh & Simmen (IBM RJ 10203, 2000)：用 EEL 与补偿打通外连接及反连接重排 {#paper-05}

- **论文来源**：Jun Rao, Bruce Lindsay, Guy Lohman, Hamid Pirahesh, David Simmen, *Using EELs, a Practical Approach to Outerjoin and Antijoin Reordering*, IBM Almaden Research Report RJ 10203, 2000 · [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/using-eels%2C-a-practical-approach-to-outerjoin-and-antijoin-reordering.pdf)（非结合律实例见 `Figure 1–2` PDF p. 6；算子冲突矩阵见 `Figure 4` PDF p. 9；补偿算例见 `Figure 6–7` PDF p. 13–14）。

Rosenthal 与 Galindo-Legaria 在 1990–1997 年间（`[RGL90]`、`[GLR97]`）进一步提炼了外连接结合律等价规则、冲突谓词集（Conflicting Sets）以及广义外连接（Generalized Outerjoin, GOJ）。然而当 IBM Almaden 团队准备将这些理论塞进 DB2 的 System R 风格自底向上动态规划（Bottom-Up DP）枚举器时，遇到了三个棘手的工程障碍：第一，`[GLR97]` 依赖对超图做复杂图分析，且只能表示语义由查询图唯一确定的简单内/外连接查询；第二，由 `NOT EXISTS` 改写而来的**反连接（Antijoin，\(\triangleright\)）**同样大量出现在商业负载中，其不对称冲突尚未纳入统一枚举；第三，GOJ 每走一步非法重排就立即插入昂贵的补偿算子，无法跨多层连接延迟合并清理。

> "This is because these three types of joins are not always associative with each other... As a result, not all orders will give the same answer as the original query, unless it is given special consideration." *(PDF p. 3)*

IBM 团队提出的解决方案异常干脆：**保留常规 DP 枚举器“合并两个不相交子计划 \(L\) 与 \(R\)（表集并集为 \(U\)）”的骨架不动，仅把谓词 \(p\) 的准入条件从常规资格列表 `NEL(p)` 升级为扩展资格列表 `EEL(p)`（Extended Eligibility List）**。

在普通内连接枚举器中，谓词 \(p\) 的 `NEL(p)` 就是它直接引用列所属的表集合（原文记表为 `TAB`），只要 \(\text{NEL}(p) \subseteq U\) 即可连接。而在含外连接与反连接的有序原始算子树中，`EEL(p)` 定义为“在不做任何事后补偿的前提下，为了算出正确结果，应用谓词 \(p\) 时 \(U\) 中必须已经到齐的全部表集合”：

> "The computation of conflicting sets requires some graph analysis and can be complicated. On the other hand, the computation of EELs requires only one traversal of the original operator tree." *(PDF p. 7)*

让我们跟着论文 `Algorithm 4.1`（PDF p. 8）走一遍单次后序遍历。每张表 \(T\) 维护一个初始为 \(\{T\}\) 的伴随集 `companion(T)`：
- 遇到内连接谓词 \(p\)：令 \(\text{EEL}(p) = \text{NEL}(p)\)；随后把 \(p\) 引用表的 `companion` 并集记为 \(W\)，并将 \(W\) 中每张表的 `companion` 更新为 \(W\)（即内连接把两侧绑成了命运共同体）。
- 遇到单侧外连接谓词 \(p\)：取 \(p\) 在**置空侧（Null-Producing Side）**所引用表的 `companion` 并集 \(V\)，令 \(\text{EEL}(p) = \text{NEL}(p) \cup V\)；外连接自身不合并 `companion`。

回到本文开篇的反例 `R LEFT JOIN (S INNER JOIN T)`：后序遍历先访问内连接 \(p_{st}\)，得到 \(\text{EEL}(p_{st}) = \{S, T\}\)，并把 `companion(S)` 与 `companion(T)` 同步刷成 \(\{S, T\}\)；接着向上访问左外连接 \(p_{rs}\)，它的 \(\text{NEL}(p_{rs}) = \{R, S\}\)，置空侧引用了 \(S\)，于是把 \(\text{companion}(S) = \{S, T\}\) 并入，得出 \(\text{EEL}(p_{rs}) = \{R, S, T\}\)！当自底向上枚举器试图先合并 \(\{R\}\) 与 \(\{S\}\) 时，子计划并集 \(U = \{R, S\}\) 无法覆盖 \(\text{EEL}(p_{rs}) = \{R, S, T\}\)，错误计划 `(R ⟕ S) ⋈ T` 在诞生瞬间就被静态门控枪毙。而对于可结合的链式左外连接 `(R ⟕ S) ⟕ T`，内层外连接不合并伴随集，各谓词的 `EEL` 均等于 `NEL`，枚举器依然允许自由交换结合！`Theorem 4.2`（PDF p. 8）证明了在已完成空值简化、谓词均为 Null-Intolerant 且不引入笛卡尔积的简单查询前提下，`EEL` 门控生成的计划集合与结合律规则可达的计划集合严格双向等价。

当 `Section 4.2` 进一步把反连接（Antijoin）拉入战场时，冲突矩阵（`Figure 4`，PDF p. 9）变得高度不对称。设上层算子与下层某侧子算子相邻，二者是否违反结合律（即是否需要扩充 `EEL`）可浓缩为如下静态冲突表：

| 上层算子类型与观察侧 | 下层是内连接 `⋈` | 下层是单侧外连接 `⟕` | 下层是反连接 `▷` | 代数根因简析 |
| :--- | :--- | :--- | :--- | :--- |
| **外连接 `⟕`（保留侧 / 左）** | 可结合 | 可结合 | 可结合 | 左表保留语义与左子树内部筛选互不干扰 |
| **外连接 `⟕`（置空侧 / 右）** | **冲突** | 可结合 | **冲突** | 右侧先置空再做内连接/反连接会误杀补空行或翻转匹配性 |
| **反连接 `▷`（保留侧 / 左）** | 可结合 | **向内指时冲突** | 可结合 | 先反连接删行会剥夺后续外连接保留行的机会 |
| **反连接 `▷`（置空侧 / 右）** | **冲突** | **冲突** | **冲突** | `NOT EXISTS` 对右侧结果集非单调，任何局部增删都会反转输出 |

因此 `Algorithm 4.2`（PDF p. 9–10）拆分出 `outer(T)` 与 `anti(T)` 两个追踪集，规定反连接谓词 \(p\) 的 `EEL(p)` 必须同时吞并“保留侧引用表的 `anti` 集”以及“**整个置空侧子树的全部表**（哪怕 \(p\) 并未直接引用它们的列）”。这里有一处精读原文才能捉住的技术细节：PDF p. 10 印刷的 `Algorithm 4.2 Part B` 伪代码在外连接更新步骤只把保留侧 `anti` 集并入了“被谓词 \(p\) 直接引用的置空侧表”，而同页 `Example 4.4` 对 `((R ⟕ (S ⋈ T)) ⟕ U) ▷ V` 给出的正确手工表值却将 \(R\) 一路传播到了 \(\text{anti}(T) = \{R, T\}\) 进而得出 \(\text{EEL}(p_{uv}) = \{R, T, U, V\}\)。若照字面伪代码漏掉向同一伴随集内 \(T\) 的传播，\(\text{EEL}(p_{uv})\) 就会少掉 \(R\)，从而放行“先在 \(\{S,T,U,V\}\) 内部反连接置空、最后被 \(R\) 左外连接补出一行假结果”的错误计划。工程落地时，外连接向置空侧传播 `anti` 依赖必须覆盖内连接伴随闭包而非仅限于直接引用表。

如果 `R` 极小（仅几行）、`S` 和 `T` 极大（上亿行），死守无补偿 `EEL` 被迫先算亿级 `S ⋈ T` 实在太亏，能不能强行先算 `R ⟕ S` 再事后把答案修对？论文 `Section 5.1`（PDF p. 13–15）给出了简单内/外连接查询的**带补偿重排（Reordering with Compensation）**三步曲：

1. **连接提升（Promotion）**：绝不能直接执行会丢行的内连接 \(p_{st}\)，必须把尚未执行的冲突内连接提升为左外连接，先算 \((R \mathbin{\text{⟕}}_{p_{rs}} S) \mathbin{\text{⟕}}_{p_{st}} T\)，确保 `R` 的未匹配行一行不少地活下来。
2. **条件置空（Nullification）**：引入算子 \(\text{Null}[\sim p_{st}, S]\)，凡是 \(p_{st}\) 未能真实匹配的行，把之前从 \(S\) 带过来的列（连同 `S.TID`）全部抹成 `NULL`，还原 `S ⋈ T` 原本不该产出该行的事实。
3. **最佳匹配消重（Best Match, `BM`）**：经过置空后，同一个左表元组若原先匹配了多条未命中 `T` 的 `S` 行，会产生多条重复的全空补齐行；而若某条左表元组既有命中 `T` 的完整行、又有未命中 `T` 被置空的残缺行，后者就成了被前者包含（Subsumed，即非空字段取同值且非空字段集合真包含于前者）的伪行。最终通过携带参与基表的元组标识符（`TID`）运行 `BM` 算子，一次性剔除重复行与被支配伪行：

$$Q = \text{BM}\Big(\text{Null}[\sim p_{st},\, S]\big((R \mathbin{\text{⟕}}_{p_{rs}} S) \mathbin{\text{⟕}}_{p_{st}} T\big)\Big)$$

不妨用一组 4 行中间态具体走一遍这个三步修补过程（设 \(R = \{r_1(1), r_2(2)\}\)，\(S = \{s_1(1, 10), s_2(1, 20), s_3(2, 30), s_4(2, 40)\}\)，\(T = \{t_1(10)\}\)，目标语义为 \(R \mathbin{\text{⟕}}_{a} (S \bowtie_{b} T)\)）：

| 执行阶段 | 产出的元组集合（附带基表 `TID` 身份） | 为什么此时还不能直接输出？ |
| :--- | :--- | :--- |
| **Step 1：连接提升** \((R \mathbin{\text{⟕}} S) \mathbin{\text{⟕}} T\) | `(r1, s1, t1)`、`(r1, s2, -)`、`(r2, s3, -)`、`(r2, s4, -)` | 虽保住了 `r2`，但混入了未通过 \(S \bowtie T\) 的脏 `S` 列 `s2, s3, s4` |
| **Step 2：条件置空** \(\text{Null}[\sim p_{st}, S]\) | `(r1, s1, t1)`、`(r1, -, -)`、`(r2, -, -)`、`(r2, -, -)` | `(r1, -, -)` 是被真实匹配遮蔽的伪行；`(r2, -, -)` 重复出现了两次 |
| **Step 3：最佳匹配** \(\text{BM}\) | `(r1, s1, t1)`、`(r2, -, -)` | `(r1, -, -)` 被 `(r1, s1, t1)` 严格支配剔除；两条 `(r2, -, -)` 按 `r2.TID` 合并为一 |

不同于早期 GOJ 每走一步非法交换就立刻执行昂贵的分组清理，这套代数允许将多个置空与 `BM` 沿树向上延迟合并为单次最终 `BM`：

$$\text{BM}\big(\text{Null}[p_2, Y](\text{BM}(\text{Null}[p_1, X](E)))\big) \;\equiv\; \text{BM}\big(\text{Null}[p_2, Y](\text{Null}[p_1, X](E))\big)$$

这样就把“多携带一段伪元组导致中间结果变胖”与“少做一次哈希分组去重”交给代价模型定量权衡；代价则是补偿状态变成了计划相关（Plan-Dependent）属性，且反连接的补偿仍留作开放难题。

---

## 第一代去相关与分布式嵌套求值：修补 COUNT 空集陷阱与跨站点执行代价

搞定了平铺算子树上的外连接与反连接重排后，我们把视线转向另一座大山：**如何把层层嵌套的关联子查询安全地拉平到同一棵代数树上？** 下图完整刻画了从早期 Kim 预分组内连接的 `COUNT(0)` 陷阱，经由 Dayal 外连接广义聚合与 QUEL 显式键域，一路演进到 HyPer 依赖连接通用解耦的技术路线：

![子查询去相关演进：从 COUNT(0) 空集陷阱到 HyPer 任意嵌套解耦](/images/diagrams/paper-unnest-count-bug-to-hyper.svg)

### Dayal (VLDB 1987)：Of Nests and Trees——融合非对称外连接与广义聚合 {#paper-15}

- **论文来源**：Umeshwar Dayal, *Of Nests and Trees: A Unified Approach to Processing Queries That Contain Nested Subqueries, Aggregates, and Quantifiers*, VLDB 1987 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/a-unitied-approach-to-processing-queries-that-contain-nested-subqueries%2C-aggregates%2C-and-quantifiers.pdf)（查询图见 `Figure 2.1 / 3.1` PDF p. 3；合法生成树见 `Figure 4.3` PDF p. 8）。

在 Dayal 发表这篇里程碑论文之前，SQL 解嵌套研究处于支离破碎的打补丁状态：Kim (1982) 的预分组连接撞上了 `COUNT` 空集陷阱；Ganski & Wong (1987) 为了修补 `COUNT` Bug，不得不引入难以物理实现的对称全外连接，并把原查询硬生生劈成两段独立查询（先物化外连接聚合结果表，再跑第二条过滤查询），彻底断送了单遍流水线执行的可能。Dayal 直指症结所在——传统关系代数只有 `Restrict`、`Project`、`Join`，根本缺乏表达重复控制、分组身份和空匹配保留的一等公民算子：

> "Existing query optimizers focus on Restrict-Project-Join queries. In practice, however, query languages such as SQL and DAPLEX have many powerful features (e.g., control over duplicates, nested subqueries, grouping, aggregates, and quantifiers) that are not expressible as sequences of Restrict, Project, and Join operations." *(PDF p. 1)*

Dayal 为此设计了一套将嵌套块、聚合与存在量词全部熔铸进单张查询图（Query Graph）和单一执行计划 DAG 的扩展代数：

1. **用 `TID` 锚定元组身份，分离 `Project` 与 `Delta-Project`**：SQL 是多重集（Bag）语言。把 `EMP WHERE Dept# IN (SELECT Dept# FROM DEPT ...)` 直接改成普通连接时，若一个部门在子查询中出现两次，该部门的员工就会被错误复制两份；反之若盲目按员工姓名 `DISTINCT` 去重，原本重名的两名不同员工又会被误杀。Dayal 规定基础关系隐式携带唯一 `TID`（或主键），`Project` 保留重复，而 `Delta-Project` 严格按 `R.*`（含 `R.TID`）消重，从而把 `IN` 精确映射为首匹配即停的 `Semijoin(R, S; J) = Delta-Project(Join(R, S; J); R.*)`。
2. **广义连接 `G-Join(R, S; X; J)` 与广义聚合 `G-Agg(R; X; fnvector)`**：`G-Join` 以保留属性集 \(X\) 为参数，将真实连接匹配行与“未匹配 \(R\) 行在 \(X\) 上去重并对其余列补 `NULL`”合并——当 \(R\) 无重复且 \(X = \emptyset\) 时退化为常规内连接，当 \(X = R.*\) 时即为**非对称左外连接**！与此同时，`G-Agg` 不仅计算 `SUM`/`AVG`/`COUNT`，还把存在量词 `EXISTS` 与 `NOTEXISTS` 统一建模为布尔聚合函数（初值分别为 `FALSE`/`TRUE`，遇见首个真实内层元组时翻转，且严格忽略补空占位行）。
3. **根治 `COUNT` 空集陷阱与流水线融合**：回到开篇零员工部门的例子，Dayal 不再先对内表孤立分组，而是沿着有向 `Outerjoin` 边先做保留外层 `DEPT`（按 `D.TID`）的非对称左外连接，紧接着送入按 `D.TID` 分组的 `G-Agg` 计算 `COUNT(E.Emp#)`（只统计非空真实内层属性/主键，令补空占位行精准产出 `0`），再串接 `Restrict(D.Budget <= 1000 * Count)`！写成扩展代数流水线即为：

$$\begin{aligned}
T_{\text{join}} &:= \text{G-Join}\big(\text{DEPT},\, \text{EMP};\; X = \text{DEPT}.*;\; J: D.\text{id} = E.\text{dept\_id}\big) \\
T_{\text{agg}}  &:= \text{G-Agg}\big(T_{\text{join}};\; X = \text{DEPT}.*;\; \text{cnt} := \text{COUNT}(E.\text{emp\_id})\big) \\
Q               &:= \text{Project}\big(\text{Restrict}(T_{\text{agg}};\; D.\text{budget} \le 1000 \cdot \text{cnt});\; D.\text{id},\, D.\text{name}\big)
\end{aligned}$$

再进一步，倘若子查询内部还写有仅引用外层表的相关过滤（例如 `SELECT COUNT(*) FROM EMP E WHERE E.Dept# = D.Dept# AND D.Loc = 'Boston'`），这个 `D.Loc = 'Boston'` 绝不能简单上提到树顶 `WHERE`——否则所有非波士顿部门都会被直接删光，而按 SQL 语义，非波士顿部门的内层子查询只是过滤结果为空集、理应拿到 `COUNT = 0` 继续参与外层 `D.Budget <= 0` 的判断！Dayal 将这类条件标记为有向边上的 **`Outerrestrict`** 并入 `G-Join` 的匹配谓词：非波士顿部门因外连接匹配失败补上 `(D.*, NULL)`，随后在 `G-Agg` 中自然算出 `cnt = 0`。

更漂亮的是物理执行收益：当外层 `DEPT` 作为嵌套循环或归并连接的外表驱动时，`G-Join` 的输出流天然已按 `D.TID` 连续成组（Grouped），执行器每扫完一个部门的匹配行就能立即封账吐出该组的 `COUNT` 并评估外层阈值，连接、计数、过滤与投影一气呵成全流水线（Pipelined）推进，无需任何中间临时表落盘：

> "Unless the optimizer can simultaneously optimize a collection of queries, it will not discover that the join, aggregation, restriction and projection can all be pipelined." *(PDF p. 6)*

为了在多层混合嵌套下探索不同连接与聚合次序，Dayal 给查询图节点赋予层级 `level`（常规边长 `0`，外连接与外层优先边长 `1`），规定合法生成树严禁出现跨层边（Straddling Edge，即 \(\text{level}(Y) - \text{level}(X) > 1\)，`Figure 4.3` PDF p. 8），并给出两向聚合移动法则：
- **提前聚合（Eager Aggregation，PDF p. 8）**：仅当聚合向量只涉及单个关系、**绝对不含 `COUNT`** 且连接条件全为等值时，才允许像 Kim 那样先在单表上做 `G-Agg` 再做连接。
- **延迟聚合（Lazy Aggregation，PDF p. 9）**：把“先对 \(R\) 聚合再与 \(S\) 连接”改写为“先连接再聚合”时，若一个 \(R\) 元组可能匹配多个 \(S\) 元组，分组键必须从 \(X\) 扩充为 \((X, S.*)\)（含 `S.TID`），否则不同 \(S\) 伙伴的扇出会把同一组内的聚合输入翻倍污染：

$$\text{G-Join}\big(\text{G-Agg}(R; X; F),\, S;\, Y;\, J\big) = \text{G-Agg}\big(\text{G-Join}(R, S; Y; J),\, (X, S.*);\, F\big)$$

需要指出的是，Dayal 在 PDF p. 10 尝试用函数依赖检测算子 `Test-FD(R; R.TID -> S.B)` 来捕捉标量子查询返回多行的运行时错误——这在内层返回两行不同值（如 `5` 和 `8`）时有效，但如果内层恰好返回两行完全相同的数值（如两行 `7`），函数依赖依然成立，SQL 标准要求的标量基数异常就会漏报；这一缺口直到后来 HyPer 引入专门的 `Single Join` 才被彻底堵严。

### Lohman, Daniels, Haas, Kistler & Selinger (VLDB 1984)：R* 分布式嵌套查询的时机与站点协同 {#paper-16}

- **论文来源**：Guy M. Lohman, Dean Daniels, Laura M. Haas, Ruth Kistler, Patricia G. Selinger, *Optimization of Nested Queries in a Distributed Relational Database*, VLDB 1984 · [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/optimization-of-nested-queries-in-a-distributed-relational-database.pdf)（分布式优化主算法与协议见 `Figure 1–5`，PDF p. 7–8）。

如果说 Dayal 代表了“尽可能将嵌套块融为一棵单机扩展代数树”，IBM San Jose 实验室的 R* 团队（包括 System R 优化器之母 Patricia Selinger 与 Guy Lohman）则在分布式数据库场景下给出了另一个极其务实的工程视角：**当各层查询块（Query Block, QB）的基础表散落在跨广域网的不同物理站点时，并不是所有子查询都能合法展平，即便能展平也未必比受控的分布式嵌套协议更省网络开销**。

在分布式环境里，嵌套查询优化撞上了一个棘手的“先有鸡还是先有蛋”悖论：自底向上规划要求先算出子查询块 \(K\) 的最优计划和代价再传给父块 \(J\)；然而子块 \(K\) 到底把结果运到哪个站点（Delivery Site），取决于父块 \(J\) 决定在哪个站点应用该嵌套谓词（Application Site）！同时，R* 的代价方程在 System R 的 CPU 指令数与磁盘页 I/O 之外，显式计入了跨站点消息次数与消息字节长度：

> "In R*, costs have been extended to include a linear combination of the number of inter-site messages and the length of inter-site messages, as well as the System R cost components of CPU (number of instructions executed) and I/O (pages accessed)." *(PDF p. 2)*

R* 用两套核心机制解开了这个死结：

首先是**精确到祖先层级的求值生命周期分类：`Evaluate-at-Open (EAO)` 与 `Evaluate-at-Application (EAA)`**（PDF p. 3–4）。很多工程师容易把子查询粗糙地二分法为“无相关（全查询只跑一次）”和“有相关（父块每行跑一次）”。R* 指出这种二分在三层以上嵌套中是错的！考虑如下三层跨站点查询（`EMP` 在站点 1、`DEPT` 在站点 2、`PROJ` 在站点 3）：

```sql
/* QB1 (站点 1) */
SELECT E.NAME FROM EMP E
WHERE  E.DNO IN (
    /* QB2 (站点 2)：自身未引用 QB1，但后代引用了 QB1！ */
    SELECT D.DNO FROM DEPT D
    WHERE  D.BUDGET > (
        /* QB3 (站点 3)：跨过直接父块 QB2，仅引用顶层 QB1 的 E.EMP#！ */
        SELECT SUM(P.COST) FROM PROJ P
        WHERE  P.MGR_EMP# = E.EMP#
    )
);
```

请看 `QB3` 与 `QB2` 各自相对父块的求值频率差异：
- 对于直接父块 `QB2` 而言，只要顶层 `QB1` 当前行的 `E.EMP#` 固定不变，无论 `QB2` 扫描多少万行 `DEPT` 候选元组，`QB3` 的 `SUM(P.COST)` 结果都是恒定的！因此 `QB3` 相对于 `QB2` 属于 **EAO**——只需在每次新 `QB1` 元组触发 `QB2` 执行 `OPEN` 时求值一次（在应用站点落地为临时值/临时表），供 `QB2` 本轮所有元组反复本地读取，绝不用按 `|EMP| × |DEPT|` 乘积次数跨网重算！
- 与此同时，相关性必须向上传递：尽管 `QB2` 自己的 SQL 文本里一个 `QB1` 的列都没写，但因为它的后代 `QB3` 依赖 `E.EMP#`，所以 `QB2` 整体相对于 `QB1` 继承了相关性，必须归类为 **EAA**（随 `QB1` 每个候选元组应用谓词时重新触发一次 `OPEN`）。

记父块候选行数为 \(N_{\text{cand}}\)、发起该相关参数的祖先块 `OPEN` 次数为 \(N_{\text{open}}\)（无相关子查询对应 \(N_{\text{open}} = 1\)），两类协议的总代价记账公式截然不同：

$$\begin{aligned}
\text{Cost}_{\text{EAA}}(K \mid A) &= N_{\text{cand}} \cdot \big(\text{Cost}_{\text{eval}}(K \to A) + \text{Cost}_{\text{msg}}(\text{corr\_params}: A \to B)\big) \\
\text{Cost}_{\text{EAO}}(K \mid A) &= N_{\text{open}} \cdot \big(\text{Cost}_{\text{eval}}(K \to A) + \text{Cost}_{\text{store\_temp}}(A)\big) + N_{\text{cand}} \cdot \text{Cost}_{\text{local\_probe}}(A)
\end{aligned}$$

其次是**携带未定站点占位符 `??` 的自底向上延迟绑定协议**（PDF p. 5–8）。子块 \(K\) 在自底向上枚举时，不仅保留在各个“自然结果站点”（本块及后代块所涉表所在站点）产出结果的最优子计划，还额外保留一个交付站点待定的 `??` 候选计划。当优化器推进到父块 \(J\)、将复合中间表 \(C\) 与新表 \(T\) 连接并使子块 \(K\) 的谓词满足列可用条件时，枚举三类应用站点选项：
- **`A1`（在表 \(T\) 所在站点提前过滤）**：仅当嵌套谓词所需的父块列全部来自单表 \(T\)（其余相关列来自更高祖先且已就绪）时合法，无需等 \(C\) 与 \(T\) 跨网汇合就能就地筛掉不合格的 \(T\) 行；
- **`A2`（在父块 \(J\) 的连接计算或最终交付站点应用）**；
- **`A3`（把父块元组推向子块 \(K\) 的自然结果站点去测试）**。

一旦选定应用站点 \(A\)，EAA 协议每测试一行父块候选元组就由 \(A\) 向求值站点 \(B\) 发送相关参数并收回单值/布尔标记（总代价按父块估计基数乘以单次含交付求值代价计费；对于相关的 `IN` 集合子查询，R* 在 `Section 6.5` 实现了向 `EXISTS` 加 `IS NOT NULL` 守卫的内部转换，命中首行立即早停返回 `FOUND`，避免跨网传回整张集合）。而对于 EAO 协议，发起块 \(I\) 的站点 \(C\) 与父块应用站点 \(A\) 可能异址：R* 设计了精巧的三角路由——站点 \(C\) 在 `OPEN` 时派生代理进程 `INIT'` 到应用站点 \(A\)，相关参数沿 \(C \to A \to B\) 传递，子块求值结果直接由 \(B \to A\) 落地为临时关系并回传 `ACK`，避免了结果集绕道 \(C\) 中转的双倍带宽浪费。不过作者也在 `Section 7` 坦诚揭示了代价：当每张表分处不同站点且靠嵌套谓词串联时，应用站点的组合会在自底向上过程中复合膨胀（例如三表嵌套查询 `Example G` 的完整评估计划数从单站点的寥寥数个暴增至 `48` 个，含局部子计划共 `64` 个）：

> "So the number of candidate plans may grow exponentially with the number of tables, if each table is at a different site and all tables are joined via a nested predicate." *(PDF p. 11)*

### Kiessling (UCB/ERL 84/75, 1984)：重审 SQL 与 QUEL 关联聚合转换的正确性裂痕 {#paper-17}

- **论文来源**：Werner Kiessling, *SQL-Like and Quel-Like Correlation Queries with Aggregates Revisited*, UC Berkeley Technical Report Memorandum No. UCB/ERL 84/75, 1984 · [原始 PDF（共 17 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/sql-like-and-quel-like-correlation-queries-with-aggregates-revisited.pdf)（`PARTS`/`SUPPLY` 反例表见 PDF p. 5–7；跨层双 `COUNT` 查询 `Q3` 见 PDF p. 9–10；Ingres `BY-list` 推演见 PDF p. 12–15）。

就在 R* 发表同一年，加州大学伯克利分校的 Werner Kiessling 提交了一份锋芒毕露的技术报告，把当时学术界奉为圭臬的两套关联聚合处理方案——SQL 阵营 Kim (1982) 的 `type-JA` 预分组去相关算法，与 Ingres 阵营 Epstein (1979) 的 QUEL `BY-list` 聚合求值算法——放到了显微镜下逐行复算。Kiessling 开宗明义地确立了不可动摇的裁判准则：任何针对关联聚合（`type-JA`）的代数重写，其输出必须与逐行代入的嵌套迭代（Nested-Iteration）参考语义完全一致：

> "The standard method for evaluating correlation queries with aggregates is by nested-iteration, evaluating the inner query block once for each substituted correlation value... By this nested-iteration procedure also the semantics of a type-JA SQL-query is defined in a convenient way." *(PDF p. 4)*

报告首先用一组五脏俱全的真实测试表（PDF p. 5）刺穿了“只要外层键值在内表出现过就不会触发 `COUNT` Bug”的侥幸心理。设零件表 `PARTS(PNUM, QOH) = {(3, 6), (10, 1), (8, 0)}`，供货表 `SUPPLY(PNUM, QUAN, SHIPDATE)` 中零件 `8` 明明有一条 `1983-05-07` 的供货记录，但内层子查询带有局部过滤条件 `WHERE S.PNUM = P.PNUM AND S.SHIPDATE < '1980-01-01'` 并比较 `P.QOH >= (SELECT COUNT(SHIPDATE) ...)`（查询 `Q2`）。零件 `8` 的 1983 年记录被内层日期谓词滤除后，其有效输入集变为空集，标量 `COUNT` 返回 `0`，满足 `0 >= 0`；然而 Kim 转换先在过滤后的 `SUPPLY` 上按 `PNUM` 分组，生成的 `TEMP'` 只有 `{(3, 2), (10, 1)}`，随后的内连接直接把零件 `8` 漏掉！

有人当时提出一种看似聪明的“最外层补零”补丁：既然 `TEMP'` 缺了零计数组，我在最外层把查询改写成“`PARTS` 内连接 `TEMP'`，或者（`P.QOH >= 0` 且 `P.PNUM NOT IN (SELECT SUPPNUM FROM TEMP')`）”不就补回来了？Kiessling 随即构造出杀手级三层双 `COUNT` 嵌套查询 **`Q3`**（PDF p. 9–10）将这种外层打补丁的幻想彻底击碎：

```sql
SELECT Ri.Cr
FROM   Ri
WHERE  Ri.Ch = (
    SELECT COUNT(Rj.Cn)
    FROM   Rj
    WHERE  Rj.Cm = (
        SELECT COUNT(Rk.Cp)
        FROM   Rk
        WHERE  Rk.Cq = Ri.Cr   -- 跨过中间层 Rj 直接关联最外层 Ri.Cr！
          AND  Rk.Cs = Rj.Ct
    )
);
```

在作者给出的 6 个外层键值 `{3, 10, 8, 5, 15, 12}` 实例上，看看 `Ri.Cr = 5`（外层要求 `Ch = 1`）与 `Ri.Cr = 12`（外层要求 `Ch = 0`）是如何同时击穿两种改写的：

| 外层键值 `Ri.Cr` | 最内层真实 `COUNT(Rk.Cp)` | 中间层真实 `COUNT(Rj.Cn)` | 外层期望 `Ri.Ch` | 嵌套迭代真值 | Kim 预分组内连接 | 外层 `NOT IN` 补零补丁 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`Cr = 5`** | `0`（`Rk` 无匹配） | **`1`**（命中 `Rj.Cm = 0` 的行！） | `1` | **入选 (`1 = 1`)** | 漏选（第一层丢失 `0`） | **仍漏选**（补 `0 ≠ 1`） |
| **`Cr = 12`** | `0`（`Rk` 无匹配） | **`1`**（同样命中 `Rj.Cm = 0`！） | `0` | **淘汰 (`0 ≠ 1`)** | 碰巧未入选 | **误入选**（误把缺失当外层 `0`！） |

现象背后的数学原因一目了然：当 `Ri.Cr = 5` 时，最内层 `Rk` 找不到匹配行产出标量 **`0`**，恰好激活了中间层 `Rj` 里等待 `Rj.Cm = 0` 的元组 `(Cn=74, Cm=0)`，令外层真实计数变为 **`1`**。而在内键预分组重写下，`Cr = 5` 和 `Cr = 12` 的零计数信息在第一层临时表 `Rt1` 中就已经烟消云散，导致第二层 `Rt2` 根本无法命中 `Cm = 0`；到了最外层再去检查“谁不在 `Rt2` 里就当外层 `COUNT = 0` 补零”，不仅救不回外层真实计数为 `1` 的 `5`，反而把外层真实计数同样为 `1`（本应因 `Ch = 0 ≠ 1` 淘汰）的 `12` 当成零计数组混入结果！**信息一旦在内层作用域因丢失空组而坍缩，外层任何补偿逻辑都无法逆向重构**。

那么另一边的 QUEL / Ingres 表现如何？QUEL 用显式 `COUNT(S.SHIPDATE BY P.PNUM WHERE ...)` 语法求值。Epstein 的经典三步法天生避开了空集消失陷阱：

```sql
-- Step 1：先提取外层关联键的去重域，并把所有键的计数显式初始化为 0！
CREATE TABLE TEMP1 AS
SELECT DISTINCT P.PNUM, 0 AS CT FROM PARTS P;

-- Step 2：用去重键域 TEMP1 连接内表 SUPPLY（保留内表自身的真实重复行！）计算非零组计数
CREATE TABLE TEMP2b AS
SELECT T.PNUM, COUNT(S.SHIPDATE) AS CT
FROM   TEMP1 T JOIN SUPPLY S ON T.PNUM = S.PNUM AND S.SHIPDATE < '1980-01-01'
GROUP BY T.PNUM;

-- Step 3：用非零计数覆盖 TEMP1（未命中键仍保持 0），最后回接未去重的原始外层表 PARTS！
UPDATE TEMP1 SET CT = TEMP2b.CT FROM TEMP2b WHERE TEMP1.PNUM = TEMP2b.PNUM;
SELECT P.* FROM PARTS P JOIN TEMP1 T ON P.PNUM = T.PNUM WHERE P.QOH >= T.CT;
```

但 Kiessling 揪出了 Ingres 实现在将这套思想落地时的两颗新地雷（PDF p. 12–15）：
- **外层重复键的计数放大**：如果省掉 `TEMP1` 的去重、在 Step 2 直接拿原始外层表 `PARTS`（假设非主键列 `P.PNUM = 10` 出现了 `2` 次）与内表 `SUPPLY`（含 `2` 条 `PNUM = 10` 的记录）连接喂给聚合器，多对多笛卡尔扇出会把聚合输入撑大成 \(2 \times 2 = 4\) 条，算出荒谬的 `COUNT = 4`！反之，若在连接结果上盲目做全元组 `DISTINCT`，内表 `SUPPLY` 里两条合法同值供货记录又会被误压成 `1` 条。正确做法必须严格遵守三原则：**连接内表时外层键去重、聚合时保留内表真实重复行、回接过滤时恢复外层原始重数**。
- **`BY-list` 范围变量消除的跨层断链**：Ingres 优化器在多阶段重写 `Q3'` 时，看到临时表列 `T1.Cr` 是外层 `I.Cr` 的完整去重投影，便自作聪明地在子查询里把 `I.Cr` 替换成 `T1.Cr`，导致跨层关联谓词 `I.Cr = T1.Cr` 退化为恒真自等式 `T1.Cr = T1.Cr` 而被删去，最终阶段丢失了按外层元组精确对号入座的纽带，多吐出了错误答案 `12`。Kiessling 还在报告末尾点出了普通单块 SQL `WHERE JOB = 'CLERK' GROUP BY DNO HAVING COUNT(*) < 10` 同样漏掉零文员部门的语义盲区：

> "However, consider the slightly changed query where we ask for department that employ less than ten clerks. Now the current WHERE - GROUP BY evaluation order fails to report departments that employ no clerks." *(PDF p. 16)*

Kiessling 所揭示的“先提取去重外层关联域、保留零组初始化、再回接原始外层多重集”这一核心范式，不仅直接启发了随后 Muralikrishna (VLDB 1992) 利用外层驱动域与共享扫描有向无环图（Shared-Scan DAG）合并同源多分支关联聚合的算法，更在三十年后成为了 HyPer 通用去相关恒等式的直接思想先声。

---

## 终极解耦与工业闭环：HyPer 任意查询去相关与完整连接家族

尽管 Dayal、Kiessling、Seshadri 与 Galindo-Legaria 陆续攻克了单层及多分支关联聚合，但直到 2010 年代中期，主流商业与开源优化器面对“析取条件（`OR`）里嵌着关联标量子查询”、“多层 `EXISTS` / `ALL` 与外连接和 `GROUP BY` 交织”等怪兽级 SQL-99 查询时，一旦匹配不上预设的几十条启发式重写模板，就只能无奈退回 \(O(N \cdot M)\) 的嵌套循环求值。慕尼黑工业大学 Thomas Neumann 与 Alfons Kemper 团队在两篇姊妹论文中，用一套完备的代数推演为这场长达三十年的拉锯战画上了句号。

### Neumann & Kemper (BTW 2015)：Unnesting Arbitrary Queries——用依赖连接下推彻底消除相关性 {#paper-19}

- **论文来源**：Thomas Neumann, Alfons Kemper, *Unnesting Arbitrary Queries*, BTW 2015 · [原始 PDF（共 20 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/unnesting-arbitrary-queries.pdf)（依赖连接下推流程见 `Figure 1` PDF p. 7；全套下推代数式见 PDF p. 8–9；等价类替换见 PDF p. 13–15）。

为什么过去的优化器总有解不开的关联子查询？因为它们试图一步到位把语法千变万化的子查询“上提（Pull Up）”成常规连接；只要子查询内部隔着聚合、外连接、集合差或者非等值析取，谓词就提不上来。Neumann 与 Kemper 彻底反转了方向：**既然把里面的东西往外提会被卡住，那就把外面的绑定域拆出来，一路向下推（Push Down）穿过子查询里的每一个算子，直到叶节点再也没有自由变量为止！**

在 HyPer 的代数表示中，任何关联求值都被统一抽象为一个逻辑算子——**依赖连接（Dependent Join，记作 \(T_1 \bowtie_p^d T_2\)）**：右侧子树 \(T_2\) 含有引用左侧属性集 \(A(T_1)\) 的自由变量集合 \(F(T_2)\)。第一阶段优化器先尝试轻量级的简单谓词上提（Simple Unnesting）；若仍有残留自由变量，立即启动第二阶段**通用去相关（Universal Unnesting）**，祭出整篇论文最核心的**域分解恒等式**（PDF p. 6–7）：

$$T_1 \bowtie_p^d T_2 \;\equiv\; T_1 \bowtie_{p \land \big(T_1 =_{\text{IS}} A(D)\big)} \big(D \bowtie^d T_2\big), \qquad D := \Pi_{F(T_2) \cap A(T_1)}(T_1)$$

请仔细品味这个等式的每一处设计巧思：
1. **局部域去重与外层多重集解耦**：投影算子 \(\Pi\) 提取出 \(T_2\) 真正用到的那几列外层自由变量并**消除重复**，形成无重复绑定域集合（Duplicate-Free Domain）\(D\)。哪怕外层 \(T_1\) 因前置连接让同一个学生 ID 出现了 10 次，\(D\) 中也只保留 1 个绑定，右侧子树 \(T_2\) 对每个不同绑定只算一次；算完后再与原封不动的 \(T_1\) 做常规连接，10 条外层行的 SQL Bag 重数分毫不损！
2. **`NULL`-safe 的 `IS` 回接语义**：回接条件 \(T_1 =_{\text{IS}} A(D)\) 采用 SQL 的 `IS NOT DISTINCT FROM` 语义，确保当外层关联列本身为 `NULL` 时，该 `NULL` 绑定也能在 \(D\) 中占有一席之地并准确接回外层行，绝不会像普通 `=` 那样把 `NULL = NULL` 判成 `UNKNOWN` 丢行。
3. **严格可控的内存上界**：由于 \(|D| \le |T_1|\)，如果去相关后的顶层连接采用以 \(T_1\) 为构建侧的哈希连接，额外物化 \(D\) 最多只让该连接的哈希内存翻倍：

> "Note that, if the original nested join was T1 ⋈_d T2, then |D| ≤ |T1|. Thus, if (after decorrelating the subquery), the top-most join is a hash-join which stores T1 in a hash table, the memory consumption for that join is at most doubled by computing D." *(PDF p. 9)*

有了无重复集合 \(D\) 这一强不变量，优化器就可以按一套机械化的代数重写表（PDF p. 8–9）把 \(D \bowtie^d \cdot\) 自顶向下层层压入 \(T_2\) 内部：

$$\begin{aligned}
D \bowtie^d \sigma_p(T_2) &\;\equiv\; \sigma_p(D \bowtie^d T_2) \\
D \bowtie^d \Pi_A(T_2) &\;\equiv\; \Pi_{A \cup A(D)}(D \bowtie^d T_2) \\
D \bowtie^d (T_a \bowtie_p T_b) &\;\equiv\; (D \bowtie^d T_a) \bowtie_{p \land \text{nat}(D)} (D \bowtie^d T_b) \quad \text{（若单支无关 } D\text{，仅推入相关支）} \\
D \bowtie^d (T_a \mathbin{\text{⟕}}_p T_b) &\;\equiv\; (D \bowtie^d T_a) \mathbin{\text{⟕}}_{p \land \text{nat}(D)} (D \bowtie^d T_b) \quad \text{（一旦 } T_b \text{ 或 } p \text{ 依赖 } D\text{，两侧必同推！）} \\
D \bowtie^d \Gamma_{A;\, a:f}(T) &\;\equiv\; \Gamma_{A \cup A(D);\, a:f}(D \bowtie^d T) \\
D \bowtie^d R &\;\equiv\; D \times R \quad \text{（基表 } F(R) = \emptyset\text{，彻底消灭 }\bowtie^d\text{！）}
\end{aligned}$$

其中最精妙的当属外连接、半连接与反连接的**不对称下推律**：若左外连接 \(T_a \mathbin{\text{⟕}}_p T_b\) 的内侧（补空侧）\(T_b\) 或连接谓词 \(p\) 依赖 \(D\)，哪怕左支 \(T_a\) 自己完全没引用外层变量，也**绝对不能只把 \(D\) 推给右支**，而必须将 \(D\) 同时复制到左右两支并按 \(D\) 自然匹配！道理一想就通：同一个 \(T_a\) 元组在绑定 \(d_1\) 下可能匹配到 \(T_b\)，在绑定 \(d_2\) 下可能匹配不到而需要补空；只有让左支先乘上 \(D\) 为每个绑定各备一份 \((t_a, d_i)\) 身份，外连接才能按绑定独立追踪谁未匹配！全外连接则无条件两侧复制 \(D\)。而对于分组聚合 \(\Gamma_{A; a:f}(T)\)，原本无 `GROUP BY` 的全局标量聚合（如前文 `SELECT COUNT(*)`）在空输入上仍须为每个 \(d \in D\) 产出一行默认值（`0` 或 `NULL`），工程实现时要么在前置 SQL 翻译层将其降级为左外连接/单值连接，要么在分组算子之前保留 \(D\) 的左外连接域，避免直接内连接抹去零计数的绑定。

来看看这套通用下推在传统优化器束手无策的析取非等值关联查询 **`Query 2`**（PDF p. 15–16）上是如何大显神威的：

```sql
SELECT s.name, e.course
FROM   students s, exams e
WHERE  s.id = e.sid
  AND  (s.major = 'CS' OR s.major = 'Games Eng')
  AND  e.grade >= (
      SELECT AVG(e2.grade) + 1
      FROM   exams e2
      WHERE  s.id = e2.sid
         OR  (e2.curriculum = s.major AND s.year > e2.date)
  );
```

由于内层 `WHERE` 用 `OR` 把等值条件 `s.id = e2.sid` 和范围不等式 `s.year > e2.date` 拧在了一起，既不能按 `sid` 单列预分组，也没法用后面要讲的等价类直接消去 \(D\)。HyPer 毫不费力地从过滤后的 `students ⋈ exams` 提取出三元无重复域 \(D(\text{id}, \text{major}, \text{year})\)，将其推过标量聚合 \(\Gamma\) 直抵 `exams e2`，化作带复合析取条件的连接 \(D \bowtie_{\text{id}=\text{sid} \lor (\dots)} e_2\) 后按 \((d.\text{id}, d.\text{major}, d.\text{year})\) 分组求均值，最后用 `IS` 语义回接外层！由于 CS 与游戏工程专业的学生总数过滤后很小，\(|D|\) 极小，PostgreSQL 9.1 跑了 `12,099 ms`，SQL Server 2014 同样无法去相关，而 HyPer 在 **`42 ms`** 内即告完成（关闭去相关则需 `408 ms`）。

更有意思的是，在绝大多数纯等值关联查询（如把上面 `OR` 分支去掉，只留 `WHERE s.id = e2.sid`）中，推到底部的 \(D \times \text{exams}\) 紧挨着 \(\sigma_{\text{sid} = d.\text{id}}\)，其实 `exams` 表自身的 `sid` 列天然就提供了绑定值，单独物化 \(D\) 再做一次连接纯属多此一举。论文 `Section 4` 提出了基于**等价类（Equivalence Classes）的域替换（Substitution）优化**：只要在子树 \(T\) 中找到与 \(A(D)\) 同属一个等值等价类的属性集 \(B \subseteq A(T)\)，就能用零连接代价的映射算子 \(\chi_{A(D):B}(T)\) 直接替代 \(D \bowtie^d T\)（对可空列保留 `IS NOT NULL` 过滤）。这是一个子集包含关系 \(D \bowtie^d T \subseteq \chi_{A(D):B}(T)\) 而非无条件等价——省掉 \(D\) 意味着放弃了用外层小集合 \(D\) 提前过滤大内表 \(T\) 的“半连接剪枝效应”（多算出的无关组会在回接顶层 \(T_1\) 时被滤掉）。因此 HyPer 将是否执行替换交给代价模型：当外层筛选极严、\(|D|\) 很小时保留 \(D\) 提前剪枝；当 \(D\) 几乎覆盖全表时果断用替换省去物化开销。在 TPC-H SF=1 标准测试中，`Q4` 从关闭去相关的 `157,616 ms` 降至 **`7 ms`**，`Q17` 从 `4,664 ms` 降至 **`9 ms`**：

> "Query 2 is more difficult to unnest, and we are not aware of any system besides our HyPer system that is able to unnest it. HyPer can execute that query in 42ms (408ms without unnesting)." *(PDF p. 16)*

### Neumann, Leis & Kemper (BTW 2017)：The Complete Story of Joins (in HyPer)——全谱系连接算子与工程实现 {#paper-18}

- **论文来源**：Thomas Neumann, Viktor Leis, Alfons Kemper, *The Complete Story of Joins (in HyPer)*, BTW 2017 · [原始 PDF（共 20 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/the-complete-story-of-joins.pdf)（完整编译流水线见 `Fig. 1` PDF p. 2；外连接与标记哈希表布局见 `Fig. 2` PDF p. 11–12；TPC-H/TPC-DS 算子矩阵见 `Table 1` PDF p. 17）。

2015 年论文解决了“如何消除依赖连接 \(\bowtie^d\)”，但要把任意一条合法的 SQL 语句滴水不漏地先翻译成那棵初始关系代数树，再去重排和物理执行，还缺最后一块拼图。许多系统（如早期 PostgreSQL 的 `SubPlan`）之所以不敢把所有子查询都翻译成关系代数，是因为一旦子查询出现在 `SELECT` 投影列表、`CASE WHEN` 分支、`OR` 析取式或者 `NOT IN` 里，传统的 `Inner` / `Outer` / `Semi` / `Anti` 四大连接根本不够表达其精确语义，只能退回表达式求值器与查询执行器互相调用的互递归（Mutual Recursion）老路——在 TPC-H SF=1 上跑一条简单的 `SELECT p_name, (SELECT l_orderkey FROM lineitem WHERE l_partkey = p_partkey ...)`，PostgreSQL 因对 20 万行 `part` 逐行全扫 600 万行 `lineitem` 整整跑了 **`26` 小时**，而打通全关系代数管线的 HyPer 单线程仅耗时 **`17 ms`**（PDF p. 18）！

> "The disadvantage of this simple, non-relational approach is that it makes many important optimizations nearly impossible. In effect, it pre-determines the execution plan of common query patterns to nested-loop-style execution with O(n^2) runtime. We argue that the well-known inner, outer, and semi joins are not enough." *(PDF p. 1)*

为了彻底消灭互递归，本文亮出了 HyPer 从 AST 降级、去相关、超图重排到哈希物理实现的完整工业蓝图，核心是补齐两个专门承载 SQL 边界语义的逻辑与物理连接算子：

1. **单值连接 `Single Join`（守护标量子查询的 0/1/多行铁律）**：SQL 标准规定标量子查询若返回 `0` 行则取值为 `NULL`，返回 `1` 行取该标量值，一旦返回 `≥ 2` 行（**哪怕这两行的数值一模一样**）必须立即抛出运行时异常！普通左外连接无法报错，而 Dayal 的函数依赖检测又会放过重复同值行。HyPer 的 `Left Single Hash Join`（`Listing 5`，PDF p. 13）在左侧构建表每个条目旁附带 1 字节匹配标志：探测侧首次命中时置标志并输出连接行，**第二次命中同一左表元组时当场抛异常中止查询**，探测结束后遍历哈希表为未标志条目补 `NULL`。只有当优化器通过主键约束或无 `GROUP BY` 全局聚合静态证明“每次相关求值至多产出 1 行”时，才会将 `Single Join` 安全降级为常规 `Left Outer Join`。
2. **标记连接 `Mark Join`（不提前丢行的三值逻辑载体）**：考虑查询 `WHERE EXISTS(...) OR p.Sabbatical = true`，或者在投影列里直接输出 `SELECT x = ANY(SELECT y FROM S)`。如果在求 `EXISTS` 时直接用 `Semi Join`，休假中但没开课的教授就会被提前滤掉，根本没机会走到 `OR` 的右分支！`Mark Join` 的做法是：**完整保留左表每一行元组，只在其末尾追加一列三值布尔标记 `marker ∈ {TRUE, FALSE, UNKNOWN}`**。只有当后续谓词下推证明 `marker` 处于顶层合取（Conjunctive）上下文且不被上层观测 `UNKNOWN` 时，优化器才把 `Mark Join` 特化为轻量的 `Semi` 或 `Anti Semi Join`。

论文 `Listing 6`（PDF p. 14–15）展示的 `Left Mark Hash Join` 堪称处理 SQL 三值逻辑与混合类型转换的教科书级状态机。以 `R.x = ANY(SELECT S.y FROM S)`（设 `R.x` 为 `numeric(6,3)`、`S.y` 为 `integer`，公共比较类型收敛到范围更紧的 `integer`）为例，构建、探测与收尾三阶段的标记流转可精确归纳如下：

| 左表键值 `R.x` 类型 | 构建期分流桶与初标 | 右表 `S = ∅`（`!hadElements`） | 右表 `S = {4}`（无 `NULL`） | 右表 `S = {4, NULL}`（`hadNull`） |
| :--- | :--- | :--- | :--- | :--- |
| **`4.000`**（可无损转 `4` 且命中） | 主哈希表 `H`，初标 `FALSE` | 收尾保持 **`FALSE`** | 探测置 **`TRUE`** | 探测置 **`TRUE`**（`TRUE` 不被覆盖） |
| **`5.000`**（可无损转 `5` 但未中） | 主哈希表 `H`，初标 `FALSE` | 收尾保持 **`FALSE`** | 保持 **`FALSE`** | 收尾由 `FALSE` 升为 **`UNKNOWN`** |
| **`3.001`**（非空但不可转整数） | 溢出表 `H[extra]`，初标 `FALSE` | 收尾保持 **`FALSE`** | 保持 **`FALSE`** | 收尾由 `FALSE` 升为 **`UNKNOWN`** |
| **`NULL`**（左键自身为空） | 空值桶 `H[null]`，初标 `UNKNOWN` | 收尾强刷为 **`FALSE`**！ | 保持 **`UNKNOWN`** | 保持 **`UNKNOWN`** |

只需对照这张状态表，SQL 里最容易踩坑的四处边角语义便纤毫毕现：
- 空集优先于 `NULL`：哪怕左键是 `NULL`，`NULL = ANY(∅)` 按全称/存在对偶定义也必须产出 `FALSE`，所以 `!hadElements` 分支专门把 `H[null]` 从 `UNKNOWN` 刷回 `FALSE`。
- 不可转精度值绝不等同于过滤丢弃：`3.001` 虽然永远不可能等于任何整数 `S.y`，但当 `S` 中混有一行 `NULL` 时，`3.001 = 4 OR 3.001 = NULL` 的结果是 `FALSE OR UNKNOWN = UNKNOWN`！因此 `H[extra]` 必须单独暂存并在 `hadNull` 时一并升为 `UNKNOWN`。

在连接重排与多线程执行层，HyPer 采用基于超图的动态规划算法 **`DPhyp`**（PDF p. 9）在无叉积的连通子图上枚举连接次序，严格遵守非内连接的超边顺序约束：

```text
// Left Mark Hash Join 收尾两行核心逻辑（对应论文 Listing 6）
if (!hadElements)
    for (auto& entry : H_null) entry.marker = FALSE;
if (hadNull)
    for (auto& entry : H_main_and_extra)
        if (entry.marker == FALSE) entry.marker = UNKNOWN;
```

同时，HyPer 为几乎所有连接类型配备了对称的 `Left` / `Right` 物理变体（约定左构建、右探测），让优化器即使不能交换逻辑保留侧，也能自由翻转物理构建侧把小表塞进哈希表。多线程并行执行 `Semi Join` 时，为防止两个探测线程同时命中同一条构建侧元组 `r` 而重复发出两次，直接用一条无锁原子 CAS 指令争抢 `marker` 的首次置位权：

> "If the semi join is executed multi-threaded, only one thread must be allowed to emit a particular r value. The easiest way to achieve this is to update the marker using atomic instructions." *(PDF p. 13)*

对于无法做等值哈希的任意不等式外连接或标记连接，HyPer 则回退到分块嵌套循环（Blockwise Nested Loop，`Section 5.8`），用区间压缩（Interval Compression）结构跨块紧凑记录右表数百万行的已匹配位图。论文 `Table 1`（PDF p. 17）统计了 TPC-H 与 TPC-DS 全部查询在优化前后的算子存活矩阵：

| 基准测试集 | 初始翻译生成的连接类型 | 经去相关、谓词下推与特化后最终存活的连接类型 |
| :--- | :--- | :--- |
| **TPC-H（22 条查询）** | `Inner`、`Left Outer`、`Single`、`Left Mark` | `Inner`、`Left/Right Outer`、`Left/Right Semi`、`Left/Right Anti Semi`、`Group Join`（`Single` 与 `Mark` 100% 被消解特化！） |
| **TPC-DS（99 条查询）** | `Inner`、`Left/Full Outer`、`Single`、`Left Mark` | `Inner`、`Left/Right/Full Outer`、`Single`、`Left/Right Mark`、`Left/Right Semi`、`Left Anti Semi`、`Group Join` |

TPC-DS 在完全优化后依然真实保留着 `Single Join` 与 `Left/Right Mark Join`，雄辩地证明这套全谱系连接家族绝非学院派炫技，而是现代工业级 SQL 引擎不可或缺的完备底座。

---

## 横向对比：从特定模式修补到通用代数解耦的演进全景

将这七篇横跨三十三年的经典文献并置在同一坐标系下，可以清晰看到查询优化器是如何一步步把“过程式嵌套循环与边界特判”驯化为“声明式集合代数与静态图约束”的：

| 论文与年份 | 核心代数 / 元数据抽象 | 覆盖的连接与嵌套范围 | 空集（`COUNT=0`）与 `NULL` 三值保障 | 连接顺序搜索自由度 | 物理执行与工程边界 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **[#04](#paper-04) Rosenthal & Reiner (1984)** | 带标签空值 `#R`、`Aug`、`Sel(P/#S)`、`Omit`、`Unif` | 单/双侧外连接、视图展开、无相关子查询 | 严格区分 `#S-monotonicity`（支配保持）与 `Null Rejection`（外连接转内连接） | 展平为多路笛卡尔积后复用既有结合次序枚举，再按交换条件反展平 | 集合语义推导；删冗余外连接表可能改变 Bag 重复数 |
| **[#05](#paper-05) Rao et al. / IBM EELs (2000)** | 静态扩展资格列表 `EEL(p)` + 提升外连接、`Null`、`BM` | 内连接、单侧外连接、反连接（Antijoin）及复杂 `ON` 谓词 | 依托 `Null-Intolerant` 前提简化；补偿路径用 `TID` 与包含偏序清除伪行 | 无补偿阶段 \(O(1)\) 次树遍历生成静态 `EEL` 接入常规 DP；支持跨层延迟 `BM` 补偿 | 反连接仅支持无补偿重排，反连接补偿与通用全外连接留作后续 |
| **[#15](#paper-15) Dayal *Of Nests and Trees* (1987)** | `Delta-Project`、`G-Join`（保留集 \(X\)）、`G-Agg`（含布尔量词） | 关联 `IN`、`EXISTS`/`NOT EXISTS`、标量聚合与混合量词 | 非对称左外连接保留空匹配外层 `TID`，`COUNT(内层非空键)` 精准还原 `0` | 无跨层边（Straddling Edge）合法生成树 + 提前/延迟聚合双向重写 | 利用外层键聚集属性实现连接—聚合—过滤单遍流水线；`Test-FD` 漏检同值多行 |
| **[#16](#paper-16) Lohman et al. / R\* (1984)** | `EAO` / `EAA` 时机分类、自然站点、`??` 待定站点、`INIT'` | 跨站点多层嵌套谓词（含祖先跨层关联与 `IN` 转 `EXISTS`） | 保留逐行/逐 `OPEN` 嵌套求值语义，`IN` 转 `EXISTS` 显式加 `IS NOT NULL` | 自底向上携带站点候选，枚举 `A1`/`A2`/`A3` 应用站点与传输方向 | 综合建模 CPU、I/O、消息数与字节数；多站点嵌套下候选计划数呈指数膨胀 |
| **[#17](#paper-17) Kiessling (1984)** | 嵌套迭代参考语义复审、QUEL `BY-list` 预留外层键域 `TEMP1` | `type-JA` 关联聚合及跨层双 `COUNT` 嵌套查询 | 证伪内键预分组与最外层补零补丁；要求外层键域先初始化 `COUNT=0` | 揭示局部投影替换（`BY-list` 优化）会切断跨层关联纽带 | 确立“连接内表时外层键去重、回接时恢复外层重数、保留内表真实重复”三原则 |
| **[#19](#paper-19) Neumann & Kemper (2015)** | 依赖连接 \(\bowtie^d\)、去重绑定域 \(D\) 下推、等价类映射 \(\chi\) | **任意嵌套位置与任意复杂度的关联 SQL 查询** | 回接采用 `NULL`-safe `IS` 语义；可空 `ALL` 与空全局聚合需保留外层域语义 | 彻底消除 \(\bowtie^d\) 退化为纯常规代数树，解锁完整选择下推与连接重排 | \(|D| \le |T_1|\) 限制哈希内存最多翻倍；是否用等价类消去 \(D\) 由代价模型权衡剪枝收益 |
| **[#18](#paper-18) Neumann, Leis & Kemper (2017)** | `Single Join`、`Mark Join`、`Left`/`Right` 变体、`DPhyp` | 投影/析取/`WHERE`/`FROM` 全子句任意子查询与全连接族 | `Single` 严格执行 0 行 `NULL`/多行抛错；`Mark` 用 `H[null]`/`H[extra]` 维护完整三值逻辑 | `DPhyp` 带超图顺序约束枚举连通分量，左右变体自由切换哈希构建侧 | 消除互递归 \(O(n^2)\) 灾难；多线程 `Semi` 靠原子 CAS 防重，非等值回退区间压缩块循环 |

今天翻开 DuckDB、Umbra / CedarDB 等现代高性能分析型引擎的优化器源码，几乎处处都能看到这条演进主线的直接投影：解析器刚生成逻辑计划，就照着 `#18` 的规则把标量子查询和量化子查询翻译成带 `Dependent Join` 的 `Single Join` 与 `Mark Join`；紧接着去相关器运行 `#19` 的通用域分解下推消除全部相关性（顺带吸收 `#15` 与 `#17` 的教训妥善保护空分组零计数与 `NULL`-safe 绑定）；最后把干净的扁平连接图交给融合了 `#05` 冲突规则的超图枚举器 `DPhyp` 挑选物理哈希连接方向。三十余年间看似繁复的无数边角 Bug，最终都归结为对**元组身份、空集默认值与三值支配关系**这三条基本公理的忠实守卫。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#04**：Arnon Rosenthal, David Reiner. *Extending the Algebraic Framework of Query Processing to Handle Outerjoins*. VLDB 1984, pp. 334–343. [PDF (10 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/extending-the-algebraic-framework-of-query-processing-to-handle-outerjoins.pdf)
2. **#05**：Jun Rao, Bruce Lindsay, Guy Lohman, Hamid Pirahesh, David Simmen. *Using EELs, a Practical Approach to Outerjoin and Antijoin Reordering*. IBM Research Report RJ 10203, 2000 (ICDE 2001). [PDF (18 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/using-eels%2C-a-practical-approach-to-outerjoin-and-antijoin-reordering.pdf)
3. **#15**：Umeshwar Dayal. *Of Nests and Trees: A Unified Approach to Processing Queries That Contain Nested Subqueries, Aggregates, and Quantifiers*. VLDB 1987, pp. 197–208. [PDF (12 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/a-unitied-approach-to-processing-queries-that-contain-nested-subqueries%2C-aggregates%2C-and-quantifiers.pdf)
4. **#16**：Guy M. Lohman, Dean Daniels, Laura M. Haas, Ruth Kistler, Patricia G. Selinger. *Optimization of Nested Queries in a Distributed Relational Database*. VLDB 1984, pp. 403–415. [PDF (13 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/optimization-of-nested-queries-in-a-distributed-relational-database.pdf)
5. **#17**：Werner Kiessling. *SQL-Like and Quel-Like Correlation Queries with Aggregates Revisited*. Memorandum No. UCB/ERL 84/75, UC Berkeley, 1984. [PDF (17 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/sql-like-and-quel-like-correlation-queries-with-aggregates-revisited.pdf)
6. **#19**：Thomas Neumann, Alfons Kemper. *Unnesting Arbitrary Queries*. BTW 2015, pp. 383–402. [PDF (20 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/unnesting-arbitrary-queries.pdf)
7. **#18**：Thomas Neumann, Viktor Leis, Alfons Kemper. *The Complete Story of Joins (in HyPer)*. BTW 2017, pp. 31–50. [PDF (20 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/the-complete-story-of-joins.pdf)

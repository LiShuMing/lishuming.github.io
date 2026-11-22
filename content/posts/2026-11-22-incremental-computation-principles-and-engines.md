---
title: "从两个增量实验到云数仓内核：DBSP、TVR 与 Dynamic Table 的实现原理与演进图谱"
date: 2026-11-22T16:30:00+08:00
lastmod: 2026-11-22T16:30:00+08:00
slug: "incremental-computation-principles-and-engines"
categories:
  - 数据库
  - 大数据
tags:
  - 增量计算
  - IVM
  - DBSP
  - Dynamic Table
  - StarRocks
  - Apache Doris
  - Databend
  - Databricks
  - Snowflake
  - BigQuery
description: "从单行更新与极值删除的语义约束出发，剖析 Z-Set、DBSP 与 TVR 等增量计算理论主线，结合 optd-incremental 与 vela-dbsp 两个端到端个人实验暴露的六堵工程墙，横向解构 Snowflake Dynamic Tables、Databricks Enzyme、BigQuery 以及 StarRocks、Apache Doris 与 Databend 的增量架构分野。"
draft: false
toc: true
math: true
---

设想一张 10 亿行的订单明细表 `orders` 关联着千万级客户维表 `customers`，上面挂着一份实时经营看板物化视图：它既要做过滤和内连接，又要按地区算出 `SUM(amount)`、`AVG(amount)`、`MIN(price)`，甚至还带着一层 `LEFT JOIN` 和按销售额排名的 `Top-100`。今天过去了十分钟，上游只涌进了 1,000 行变更——里面夹着一笔把某条订单金额从 `100` 改成 `90` 的更新，还有一笔恰好把华东区当前最低价订单给删掉了的退款。

如果按批处理的老办法把整条 SQL 推倒重跑一遍，扫完 10 亿行只为了消化这 1,000 行改动，99.9999% 的 I/O 和 CPU 都在重复做昨天已经做过的功。既然如此，最朴素的想法当然是“只把这 1,000 行增量喂进查询算子”。可刚把数据往里送，结果立刻就错了：
- `orders ⋈ customers` 里新来的订单能连上维表里的旧客户，但如果只拿两边的新增变更做连接，新订单永远撞不见早就躺在维表里的老客户；
- 金额从 `100` 改成 `90` 的订单，如果只往下游吐一条 `90`，地区总销售额就凭空多加了 `90` 而没减去旧的 `100`；
- 华东区当前最低价被删掉之后，仅凭旧的 `MIN` 标量结果，引擎根本无从得知“排在第二低的价格是多少”，除非重新翻找该地区剩下的全部订单；
- `LEFT JOIN` 右表某条匹配记录刚被删光，左表那行原本正常输出的记录不仅要撤回，还得凭空长出一条右列全填 `NULL` 的补齐行。

这几个看似琐碎的边界瞬间暴露了增量计算（Incremental View Maintenance，IVM）最核心的物理账单：**增量计算从来不是凭空省掉了计算量，而是用“常驻的历史状态（State）+ 精确的变更代数 + 版本契约”，去换取免于重复扫描全量历史数据的代价。** 前一篇 [CDC 如何从表中产生：从状态、变化到增量执行]({{< relref "2026-08-25-dive-cdc.md" >}}) 拆解了前半段——列存与湖仓表如何把变化变成可靠的 `+I / -U / +U / -D` 事件流；这一篇沿着下游走完后半段：变更离开表之后，查询引擎究竟怎样把一条 SQL 变成增量执行计划？

过去一段实践里，我先后写了两个风格互补的个人增量实验原型——一套长在 Rust/DataFusion 生态里的 `optd-incremental` / `optd-incremental-sql`，另一套跑在 Python + 真实 Cascades 优化器 + Iceberg v2 + DuckDB 对拍上的 `vela-dbsp`。亲手把教科书公式从单算子写到 TPC-H / TPC-DS 端到端闭环、再撞上死状态膨胀、浮点撤回失效、`Sort/Limit` 悬崖与 Checkpoint 写放大之后，再回头看学术界的 DBSP、TVR/Tempura，以及工业界的 **Snowflake Dynamic Tables、Databricks Enzyme、BigQuery** 和开源阵营的 **StarRocks、Apache Doris、Databend**，各家看似大相径庭的架构抉择，其实全都是对同一组约束的不同答卷。

---

## 一、第一性原理：代数可微性与算子的三档状态账单

要把“增量更新”从零散的手工 `MERGE` 脚本变成通用的编译器能力，第一道门槛是**如何表示一次变化**。

如果只用“新增行列表”表达变化，删除和更新就无处安放；如果用 `(row, action)` 操作码（`INSERT`/`UPDATE`/`DELETE`）表达变化，同一个批次里同一行被反复修改多次，算子就必须小心翼翼地维护操作顺序。数学上最干净的抽象，是把一张表和它的每一次变更都统一成**带符号整数权重的多重集（Z-Set / Oriented Multiset）**：每一行 \(r\) 映射到一个整数权重 \(w \in \mathbb{Z}\)。合法的物化表快照要求所有行权重非负（\(w \ge 0\)），而一次变更 \(\Delta\) 则允许出现负权重——插入是 \(+1\)，删除是 \(-1\)，把订单 `42` 的金额从 `100` 改成 `90` 则是：

$$
\Delta A = \{ (42, 100) \mapsto -1,\ (42, 90) \mapsto +1 \}
$$

这种表示立刻带来两个工程红利：第一，**加减消元免费**——如果同一批次内某行先从 `100` 改到 `90` 又改回 `100`，所有权重相加归零自动抵消，彻底消除批次内更新顺序依赖；第二，**带符号多重集构成阿贝尔群（Abelian Group）**，减法永远封闭，我们可以严格定义任意查询算子 \(Q\) 的“增量导数” \(Q^\Delta\)：

$$
Q(A + \Delta A) = Q(A) + Q^\Delta(A, \Delta A)
$$

顺着这条等式把 SQL 里常见的关系算子逐个求导，整个关系代数立刻按**状态账单**自然分化成三个截然不同的梯队：

![增量计算第一性原理：Z-Set 符号权重与三类关系算子的状态账单](/images/diagrams/ivm-zset-algebra-and-operator-taxonomy.svg)

### 1.1 线性算子：零状态直通，但怕非确定性

`Filter`（\(\sigma_p\)）、`Projection`（\(\pi_c\)）和 `UNION ALL`（\(\cup\)）都满足多重集同态性（Linearity）：

$$
\Delta(\sigma_p(X)) = \sigma_p(\Delta X), \qquad \Delta(\pi_c(X)) = \pi_c(\Delta X), \qquad \Delta(X \cup Y) = \Delta X \cup \Delta Y
$$

它们完全不需要记住过去见过什么行，维护复杂度严格是 \(O(|\Delta|)\)。唯一的硬前提是**表达式必须确定性**——只要投影或过滤里混进了 `NOW()`、`RANDOM()` 或非确定性 UDF，同一行在插入时和撤回时算出的结果对不上号，负权重就再也抵消不掉正权重，视图里会永远残留幽灵脏行。

### 1.2 双线性算子（Inner Join）：为什么 `Δ` 与 `Σ` 必须并存？

一旦进入两表内连接 \(A \bowtie B\)，增量世界里最重要的结构事实就浮现了：

$$
\Delta(A \bowtie B) = (\Delta A \bowtie B_{\text{old}}) \cup (A_{\text{new}} \bowtie \Delta B) = (\Delta A \bowtie B_{\text{old}}) \cup (A_{\text{old}} \bowtie \Delta B) \cup (\Delta A \bowtie \Delta B)
$$

注意等式右边出现了两个不可省略的要素：
1. **快照（\(\Sigma\)）与增量（\(\Delta\)）必须并存**：新来的 \(\Delta A\) 必须去撞完整的历史表 \(B_{\text{old}}\)，而新来的 \(\Delta B\) 必须去撞完整的 \(A_{\text{new}}\)（或 \(A_{\text{old}}\) 外加交叉项 \(\Delta A \bowtie \Delta B\)）。**不存在任何只靠两边增量就能算出连接增量的魔法公式**——如果左右表在同一个刷新窗口里同时变动，漏掉交叉项 \(\Delta A \bowtie \Delta B\) 就会少算新旧交汇行，而两侧同时拿新快照探测又会把交叉项重复算两遍。
2. **状态体积立刻变成 \(O(|A| + |B|)\)**：为了避免每次收到几行 \(\Delta A\) 就全表扫描 \(B\)，增量引擎必须为连接双侧按 Join Key 维护持久化索引（在 Differential Dataflow 和 DBSP 中称为 Arranged Trace）。维护单轮代价降至 \(O(|\Delta A| \cdot \text{deg}_B + |\Delta B| \cdot \text{deg}_A)\)——但请注意这个 \(\text{deg}\) 因子：如果变更正好砸中倾斜热点键，或者一个批次里改了整张表 40% 的数据，增量探测 + 更新双侧索引的总开销很容易反超直接跑一次全量 Hash Join。

### 1.3 非线性算子：真正的复杂度与悬崖都在这里

越过内连接，真正的工程分水岭才刚拉开帷幕：
- **可逆群聚合（`COUNT`、`SUM`、精确整数/定点数 `AVG`）**：把 `AVG` 拆成 `(SUM, COUNT)` 两个独立状态槽，收到权重为 \(w\) 的行 \(x\) 时，只需更新 `sum += w * x`、`count += w`，完全不必保留历史明细行，每组状态是 \(O(1)\)。但**浮点数（`DOUBLE`）的 `SUM`/`AVG` 是个隐蔽陷阱**：先加 \(10^{16}\) 再加 \(1\) 最后撤回 \(10^{16}\)，IEEE 754 舍入误差会让结果永远丢掉那个 \(1\)，无法精确回撤。
- **过零点跃迁算子（`DISTINCT`、`LEFT/FULL OUTER JOIN`、`SEMI`/`ANTI JOIN`）**：集合去重和外连接不是线性的，它们只在**重数或匹配数跨越 `0 ↔ 1` 阈值**时才改变输出形态。例如 `LEFT JOIN` 中左行 \(l\) 原本有 2 条右表匹配，删掉 1 条只需撤回对应的连接行；只有当匹配计数从 `1` 跌到 `0` 时，才要额外发射一条 `(l, NULL)`；反之从 `0` 升到 `1` 时，必须先撤回 `(l, NULL)` 再发射匹配行。如果带非等值残余条件（Residual Predicate），就得按完整行粒度维护匹配计数。
- **不可逆极值（`MIN` / `MAX`）**：新插一个更大的值能立刻更新 `MAX`，可一旦当前最大值被撤回，仅靠单标量状态完全不知道次大值是谁。要免于整组重扫，必须在每个分组内维护一张**有序值索引或带重数表的懒删除堆**，把单次更新控制在 \(O(\log D)\)（\(D\) 为组内不同值个数）。
- **`Sort` / `Limit` / 全局窗口（增量悬崖）**：全表取前 100 名时，删掉第 37 名就必须把第 101 名补上来——这意味着算子几乎得在有序结构里留住整张上游输出。一旦遇到全局无分区排序和 `LIMIT`，绝大多数增量引擎都会在这里撞上收益拐点。

---

## 二、从学术界到工程界：五条增量理论主线的合流

理解了上面三档算子的账单，过去三十年数据库学术界关于增量计算的演进脉络就不再是一串孤立论文名，而是一条清晰的问题求解链：每代理论都在回答上一代留下的致命短板。

1. **经典计数 IVM 与 DBToaster（VLDB 2012）**：早期 Gupta & Mumick 给出了基于多重集计数的代数微分规则，把一次查询改写成增量查询。但面对多表连接，一次微分之后的增量查询里仍然带着多路连接。**DBToaster** 走得更极致：既然增量本身还是个查询，那就对增量再求二阶、三阶高阶微分（Higher-order Delta），把所有中间子查询递归编译成一组相互触发的物化 Map。它在超高频单行点更的金融风控场景快到极致，但代价是中间物化图数量随表数指数膨胀，状态内存难以承受大规模数仓多路 Join。
2. **Differential Dataflow（CIDR 2013 / Materialize）**：传统流计算假设时间严格单调递增，一旦遇到图遍历、递归 CTE 或乱序晚到数据，增量差分立刻失效。Frank McSherry 提出用**偏序逻辑时间戳（Partially Ordered Timestamps）**给每条带权重记录打标签，并通过可共享的有序日志索引 **Arranged Trace** 让多个下游算子复用同一份物化索引。这套引擎催生了工业级流数据库 **Materialize**，但也因为偏序时间与通用增量图运行时复杂，学习与运维门槛较高。
3. **TVR 与 Tempura（SIGMOD 2019 / VLDB 2020–2021）**：学术界之前长期默认“增量一定比全量快”，可云数仓生产环境天天上演反例——某天上游回刷了半张分区表，硬走增量反而比全量慢十倍。*One SQL to Rule Them All* 提出 **时变关系（Time-Varying Relations，TVR）** 将流与批统一为随时间演化的关系快照序列；紧接着 **Tempura** 提出 **TIP（Time-Varying Incremental Plan）** 优化框架，第一次把**全量快照算子（\(\Sigma\)）与增量算子（\(\Delta\)）放进同一个 Cascades 风格代价优化器（CBO）搜索空间**，让优化器按变更比例与算子代价自动决定每一层是增量维护、全量重算还是混合执行。
4. **DBSP（VLDB 2023 Best Paper / Feldera）**：即便有了代数规则，以往给复杂嵌套查询、去重、窗口和递归写增量计划依然高度依赖手写特例。**DBSP** 引入了极度凝练的流代数：把数据库演化看作 Z-Set 构成的无限流，定义**积分算子 \(I\)**（从变更流累加回快照流）、**微分算子 \(D\)**（从快照流相邻做差抽出变更流）和**单位延迟 \(z^{-1}\)**。借助链式法则 \((Q_1 \circ Q_2)^\Delta = Q_1^\Delta \circ Q_2^\Delta\)，任意复杂 SQL 都能被模块化地自动翻译成增量电路，且线性算子直接满足 \(L^\Delta = L\)、双线性连接直接套用 \((a \bowtie b)^\Delta = a \bowtie z^{-1}(I(b)) + I(a) \bowtie b\)。Rust 实现的开源引擎 **Feldera** 正是这套理论的直系工业化载体。
5. **半环视角的统一反思（Semirings in IVM，2026）**：近期关于半环在 IVM 中角色的理论梳理进一步澄清了一个常被混淆的前提——同样叫“增量维护”，维护**布尔集合存在性**（\(\mathbb{B}\)）、**整数多重集计数**（\(\mathbb{Z}\)）还是**完整血缘注释（Provenance Semiring）**，对应完全不同的代数结构。在集合语义下幂等的操作，换到多重集计数或外连接场景就必须精确追踪重数，绝不能混用规则。

| 理论流派 | 核心抽象 | 时间 / 版本模型 | Join 与非线性算子处理 | 代价优化视角 | 代表工业实现 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **经典 IVM / DBToaster** | 代数微分 / 高阶递归 Delta Map | 离散单调更新序列 | 递归物化所有高阶连接中间视图 | 偏向静态编译，假设极小 \(\Delta\) | DBToaster 编译器 |
| **Differential Dataflow** | `(data, time, diff)` + Arranged Trace | 偏序多维时间戳（支持循环迭代） | 共享有序 Trace 索引 + 差分重放 | 运行时共享索引摊销，少做批式回退 | Materialize |
| **TVR & Tempura** | 快照 \(\Sigma\) 与增量 \(\Delta\) 的时变关系 | 版本区间 `[v_old, v_new]` | 规则推导 + 任意子树全/增自由切分 | **统一 CBO 代价空间**动态选最优计划 | 阿里云 MaxCompute Delta Live MV、StarRocks TVR |
| **DBSP** | Z-Set 流 + 算子 \(I\) / \(D\) / \(z^{-1}\) | 离散同步 Epoch 时钟 | 链式法则自动增量，双线性显式拆项 | 偏流式电路构造，正与 CBO 结合 | Feldera、`vela-dbsp` |
| **Semirings in IVM** | K-关系与半环代数 | 状态映射 | 明确区分集合、重数与血缘的维护上界 | 提供正确性边界与复杂度下界判据 | 理论基座 |

---

## 三、把理论亲手落地：从 `optd-incremental` 与 `vela-dbsp` 撞出的工程真相

论文读起来总像行云流水，但真自己动手写一遍，立刻会发现魔鬼全躲在论文省略号后面。过去一段时间里，我用两套不同语言、不同出发点的个人实验把增量计算的完整闭环各自走了一遍——把它们并排摆在桌面上，恰好构成了从学术公式通往生产云数仓的最佳桥梁。

### 3.1 实验 A：`optd-incremental` → `optd-incremental-sql`（从代数下推到状态管理深水区）

第一套实验最初长在 Rust 查询优化器 `optd` 之上（[`optd-incremental`](https://github.com/cmu-db/optd) 扩展分支，本地基线 `f201a6e`）。最初的目标非常纯粹：验证能不能像写普通 Cascades 转换规则一样，在视图根节点套一个变化标记 `Δ`，通过规则引擎把 `Δ` 逐层下推到基表 `Scan` 上。

在 `optd-incremental/src/rules.rs` 里，这种纯粹的代数之美一目了然：
- 线性算子直接透传：`Δ(σ_p(X)) = σ_p(ΔX)`、`Δ(π(X)) = π(ΔX)`、`Δ(X ∪ Y) = ΔX ∪ ΔY`；
- 内连接在**不要求底层存储支持历史版本快照**的前提下，直接用当前表与当前增量的差构造旧快照 \(\Sigma(B, \text{before}) = B \ominus \Delta B\)（即 `Union(B, Negate(ΔB))`），再拼出 `Join(ΔA, B_before) ∪ Join(A_after, ΔB)`；
- 聚合算子作为终结规则（Terminal Rule），先对增量做局部聚合，与已物化视图 `StoredView` 合并后减去旧视图（`MergeAgg(Agg(ΔX), StoredView) ⊖ StoredView`），守住“每条规则都严格输出净 Delta”的统一契约；
- 最关键的一条护栏是 **Totality or Failure（全解析或显式失败）**：只要重写结束后计划树里还残留任何未消解的 `Δ` 节点，必须立即报错拒绝执行。因为一条“部分重写成功”的计划照样能跑通，却会悄无声息地把未重写子树的输出变成错误数据。

但只要把视野从玩具代数推向真实 SQL（TPC-H / TPC-DS），纯无状态重写立刻不够用了——外连接、半/反连接、`MIN/MAX`、`COUNT(DISTINCT)`、`STDDEV`、全局 `RANK()` 和持久崩溃恢复一个都绕不过去。于是实验进一步演化出独立的 Rust 引擎 **`optd-incremental-sql`**（仅状态子系统就超过 7,300 行 Rust 代码），在 DataFusion 逻辑计划之上建立了一套显式的 **8 级维护策略谱系（`Strategy`）**：

| 策略等级 | 维护机制 | 底层专属内存状态 | 在 TPC-H / TPC-DS 中的角色 |
| :--- | :--- | :--- | :--- |
| **`NativeDelta`** | 线性带符号增量直通 | 理论上无需算子状态 | 覆盖全图 **72.5%–74.2%** 的扫描/过滤/投影节点 |
| **`IndexedDelta`** | 双侧键值索引探查内连接 | `KeyedState`（按连接键分桶） | TPC-H 51 处 / TPC-DS 521 处等值内连接 |
| **`MatchCountDelta`** | 过零点匹配计数跃迁 | `KeyedState + ResidualState` | 覆盖全部 7 种外/半/反连接及残余条件（TPC-H 12 / TPC-DS 106） |
| **`AccumulatorDelta`** | 可回撤增量累加器 | `AccumulatorState`（含精确整 `AVG`、`ValueIndex` 极值、`Distinct` 引用计数、`Moments` 方差） | TPC-H 29 处 / TPC-DS 258 处聚合 |
| **`RankDelta`** | 有序等值 Peer 桶后缀更新 | `RankState`（按排序键有序 AVL） | 全局 `RANK()`（如 TPC-DS Q44 / Q49 共 8 处） |
| **`ChangedGroupRecompute`** | 仅重算受影响分组的完整输入 | `GroupState` | 不可直接累加回撤的复杂分组聚合 |
| **`KeyedRecompute`** | 仅重算受影响分区并做前后差 | `KeyedState` 输出缓存 | 分区窗口算子（TPC-DS 18 处） |
| **`OperatorRecompute`** | 重算当前算子并 diff | 节点关系缓存 | 兜底非增量算子（严格模式下显式拒绝） |

所有内存状态都压在一棵基于 `Arc` 的 **写时复制（COW）持久化有序 AVL 树 `StateDirectory<V>`** 之上：事务候选隔离只需 \(O(1)\) 克隆根节点指针，单次键值更新只拷贝 \(O(\log N)\) 路径节点。落盘层则采用 256 行分块的内容寻址 Arrow 块存储 `RowBlocks`（每次提交只写脏叶块，写放大 \(O(|\Delta|)\)），配合每 \(K=16\) 轮导出的算子状态 Checkpoint 与**有界重放恢复**——重启时不再扫全表，而是用头快照减去待重放增量之和反推出 Checkpoint 时刻的基表状态（\(B_{\text{ckpt}} = B_{\text{head}} \ominus \sum \Delta_i\)），最多重放 \(K-1\) 轮 `table_deltas` 即可追平。最终在 `relational_core`（剥离外层展示型 `Sort`/`Limit` 后）模式下，跑通了 **TPC-H 22/22 与 TPC-DS 99/99 全部 100% 严格增量编译**。

### 3.2 实验 B：`vela-dbsp`（把 DBSP 塞进真实 Cascades 优化器 + 真实 Iceberg 对拍）

如果说 `optd-incremental-sql` 重点趟平了“状态容器与 SQL 复杂语义覆盖”，那么另一套 Python 实验 **`vela-dbsp`**（本地基线 `b4a2d7a`）则把显微镜对准了另一个关键问题：**当增量电路本身存在多种等价物理实现时，真正的 Cascades 优化器该如何基于代价做选择，并如何对抗真实湖仓（Apache Iceberg v2）的快照演化？**

`vela-dbsp` 的设计非常克制而聚焦：
1. **将物理增量实现纳入真实的 `CascadesPlanner`**：许多流引擎只靠硬编码规则把逻辑算子 1-to-1 翻译成流算子。`vela-dbsp` 在 `cascades_backend.py` 里把逻辑视图（`logical_view`）与物理实现彻底剥离，让 Cascades 规则引擎同时搜索谓词/投影规范化、有界连通子集 DP 的内连接重排（单连通分量最多 7 张表，超出则退化为确定性连通贪心）、连接物理算子（嵌套循环 vs 键分区索引连接），以及聚合算子的三种物理实现——`retractable_exact`、`retractable_extrema` 和 `group_rescan`。
2. **三维逻辑工作量代价模型（`DBSPCostEstimator`）**：不同于只看单次查询耗时的批处理 CBO，增量优化器必须同时算三本账——**首次构建（`bootstrap`）+ 长期增量维护（`maintenance` × 轮次权重）+ 常驻状态内存（`state` × 状态权重）**：
   - `retractable_exact`（精确 `COUNT`/`SUM`/`AVG`）：单轮维护 \(O(\Delta)\)，非 `DISTINCT` 场景零明细行状态；
   - `retractable_extrema`（含 `MIN`/`MAX`）：在 `ExtremaAccumulator` 内为每组维护非空值重数表 + 懒删除 `heapq` 最小堆（`MAX` 用自定义类型比较器 `_MaxKey` 统一支持 `VARCHAR`/`DATE`/`DECIMAL` 而非简单取负），当堆内墓碑膨胀到 `len(heap) > 2 * len(counts) + 64` 时触发 `heapify` 压缩，把空间锁死在 \(O(D)\)、均摊更新锁死在 \(O(\log D)\)。实测在 1,000 行分组里删除当前极值时，参照路径 `group_rescan` 被迫重扫剩余 **999 行**，而 `retractable_extrema` 的历史明细重扫次数为 **0**；
   - `group_rescan`：面对浮点 `SUM`/`AVG` 或 `single_value` 等无法保证精确回撤的算子，优化器与执行器采取 **Fail-Closed** 策略，显式回退到受影响分组重扫，绝不拿精度换速度。
3. **真实 Iceberg v2 快照 + DuckDB 独立神谕（Oracle）+ 零重放 Trace Checkpoint**：所有用例都跑在真实的 PyIceberg v2 局部表上，专门注入 Append、Copy-on-Write 更新，以及**内容完全等价的文件重写（COW Rewrite，文件全换了但逻辑净增量为 0）**；每个 Epoch 的输出全量袋与带符号 Delta 都和单线程 DuckDB 1.1.3 执行原 SQL 的结果做精确多重集比对（甚至专门规避了 Arrow 14 + DuckDB 1.1.3 在 Decimal 谓词下推上漏行的已知坑）。重启时直接加载原子提交的 `vela-dbsp-trace-v1` 算子 Trace Checkpoint，做到 **`replayed_source_rows = 0` 的零重放恢复**。

### 3.3 亲手写完两个实验后，真正暴露出的四堵工程墙

正因为两个实验都跑到了真实 TPC 查询与崩溃恢复层，它们坦诚地暴露出四堵在纯理论论文里极易被轻描淡写、却直接决定了工业界云数仓长相的“硬墙”：

1. **墙一：死关系状态膨胀（Dead Relation State）**。在 `optd-incremental-sql` 的状态复盘（`rfc_state_management.md`）里有一个极具冲击力的实证数字：在 TPC-H（356 个算子节点）和 TPC-DS（3,311 个节点）中，**72.5%–74.2% 的节点都是纯线性 `NativeDelta` 算子**，下游只读它的 `DeltaRead`，从来没读过它的全量快照。如果引擎图省事为全图每个节点都缓存一份 `RowBlocks`，直接带来 **3×–5× 的无谓内存和 Checkpoint 膨胀**；更糟的是，如果每张物化视图各自保存一份基表副本，建 \(M\) 个视图基表内存就按 \(O(M \cdot |T|)\) 倍增。工业引擎必须做静态活跃度分析（只物化被 `SnapshotRead` 引用的节点），并跨视图共享同一份底表存储。
2. **墙二：SQL 语义深处的正确性暗礁**。写完 `MatchCountDelta` 才深切体会到：普通反连接（`ANTI JOIN`）的依赖局限于相等的键桶内，但 **NULL-aware Anti Join（`NOT IN`）存在全局右表 NULL 依赖**——只要右表任意一行变成 `NULL`，左表全量结果瞬间清空，局部键闭包根本无法证明其正确性，必须单走全局状态或严格拒绝！同样，浮点累加不可逆、Iceberg COW 重写产生虚假文件差异，这些细节只要漏掉一条，增量视图就会在静默中悄然漂移。
3. **墙三：算子 Checkpoint 写放大与超内存天花板**。`optd-incremental-sql` 虽然把表快照做成了 \(O(|\Delta|)\) 的脏块增量落盘，但每 \(K\) 轮持久化算子状态（`KeyedState` / `AccumulatorState`）时仍不得不把整棵算子状态树全量序列化成新 Arrow IPC 文件（写放大 \(O(|S|)\)），且重启必须全量加载进内存。一旦面对 PB 级数仓里上亿个 Group Key，内存常驻状态直接撞墙——这正是为什么大数仓宁可把算子状态“伪装”成底层列存/Delta 表，也不愿全塞进单机内存堆。
4. **墙四：`Sort` / `Limit` 悬崖与 100% 严格增量的幻觉**。在 `optd-incremental-sql` 的严格评测中，去掉外层 `Sort`/`Limit` 的 `relational_core` 模式能覆盖 100% TPC-H/TPC-DS，可一旦切回 `original` 原始 SQL，**TPC-H 有 18/22、TPC-DS 有 94/99 仅仅因为顶层 `Sort`/`Limit` 退化为 `OperatorRecompute` 而被拒绝**！这个反差一针见血地解释了：**为什么工业界没有任何一家云数仓敢把“全 SQL 100% 强制纯增量”当作产品承诺，反而都把重心放在了“刷新模式分级 + 代价自动切换 + 边界冻结”上。**

---

## 四、云原生数仓的三条工业路线：Snowflake、Databricks 与 BigQuery

带着两个实验暴露出的这四堵墙去看三大云数仓巨头，你会发现它们既没有照搬学术界的纯内存 DBSP，也没有停留在笨重的每日全量 T+1，而是沿着各自的存储底座和客户工作负载，切出了三条极具代表性的工业路线。

### 4.1 Snowflake Dynamic Tables：用“延迟视图语义”降维流计算，用 `FROZEN WHERE` 止血历史重算

在 Dynamic Tables 出现之前，Snowflake 用户要自己把 **Streams（变更捕获）+ Tasks（定时调度）+ 手工 `MERGE` SQL** 缝成一张 DAG。这套命令式管道虽然可控，但一旦链路拉长到几十层，水位对齐、故障重试和上游重算的运维复杂度迅速失控。

Snowflake 在 SIGMOD 论文中提出的破局思路，叫作 **延迟视图语义（Delayed View Semantics，DVS）**：业务上真正需要毫秒级流处理的场景其实很少，90% 的数仓加工只需要**有界的新鲜度保证（例如 `TARGET_LAG = '5 minutes'`）**。把延迟要求从“亚秒持续推送”放宽到“秒至分钟级微批增量刷新”后，整个系统瞬间获得了三项巨大收益：
1. **复用存算分离弹性池，无需常驻流式状态集群**：Dynamic Table 底层直接复用与 Streams 同源的微分区 Change Tracking 元数据（隐藏列与行追踪），每次到点醒来只读取 `(last_version, current_version]` 的变更区间，通过 IVM 增量计划算出净 Delta 并原子更新目标表微分区——没有常驻的 Flink RocksDB 节点需要 24 小时养着。
2. **显式暴露刷新模式契约（`INCREMENTAL` / `FULL` / `AUTO`）**：面对前面提到的非确定性函数、`LIMIT` 或复杂外连接，Snowflake 绝不隐藏边界。`INCREMENTAL` 模式下不满足可微条件的 SQL 直接报错拒绝；`AUTO` 模式在定义期分析查询结构，支持 IVM 则走增量，不支持或代价不划算时平滑落回 `FULL`（近年还演进出支持自定义合并逻辑的自适应增量选项）。
3. **用不可变约束（`IMMUTABLE WHERE` / `FROZEN WHERE`）根治回填风暴**：生产环境里增量管道最痛的往往不是日常运行，而是“加了一个字段、修了一行维表逻辑，或者业务回刷历史”，导致整张动态表被迫重新初始化。Snowflake 允许用户显式声明例如 `FROZEN WHERE (txn_time < CURRENT_TIMESTAMP() - INTERVAL '1 day')`，向引擎承诺**冻结边界之前的历史行永远不再变更**——初始建表后，后续所有刷新和演化都会直接跳过冻结分区，把计算账单死死钉在活跃时间窗口上。

### 4.2 Databricks Lakeflow Declarative Pipelines & Enzyme（SIGMOD 2026）：湖仓状态表拆分与 DAG 全局寻优

Databricks 面对的物理世界和 Snowflake 略有不同：它的底座是对象存储上的开放表格式 **Delta Lake**，客户跑的是超大规模 Spark / Photon 声明式 ETL 管道（从早期的 Delta Live Tables 演进为今天的 **Lakeflow / Spark Declarative Pipelines**）。它的核心增量引擎 **Enzyme**（在 SIGMOD 2026 工业界论文中系统披露）精准命中了前面实验暴露的两大难题：**状态放哪儿**，以及**谁来算全局总账**。

1. **顶层视图 + 底层 Delta 状态表的拆分架构**：如果像传统内存流引擎那样把 `AVG` 或去重计数塞进独立 KV 引擎，和湖仓的弹性无状态集群格格不入。Enzyme 的解法极其巧妙：在 Unity Catalog 里，每个物化视图被拆成**对外暴露的顶层逻辑视图**和**一张隐藏的底层 Delta 状态表**。底层表存的并不只是最终结果，而是**为增量更新专门设计的分解中间态**——比如把 `AVG(x)` 拆成 `SUM(x)` 和 `COUNT(*)` 持久化在 Delta 列里，读取时顶层视图做一次轻量除法，刷新时则直接借力 Delta Lake 的 **Change Data Feed（CDF）、Row Tracking（行追踪）与 Deletion Vectors（删除向量）** 精准合并变更，连状态本身的容错与压缩都一并交给了对象存储。
2. **打破单节点贪心：沿整张 MV DAG 算全局代价**：正如我们在 [数据库前沿观察]({{< relref "2026-09-19-database-research-frontiers.md#41-enzyme-与-semirings上游图省事全量重算下游可能跟着陪葬" >}}) 中拆解过的经典案例——假设存在级联视图 `基表 → V1 → V2`，今天基表来了一批中等规模更新，单看 `V1` 自己，增量刷新代价是 `10` 而全量重算只要 `8`。如果优化器像单查询 CBO 那样短视地让 `V1` 走全量重写，`V1` 的细粒度 Delta 血缘就被彻底抹平，下游 `V2` 被迫跟着吞下全量输入，刷新代价暴涨到 `20`（总账 `8 + 20 = 28`）；反观上游忍痛花 `10` 走增量，吐出极小净变更，`V2` 只花 `3` 就能收工（总账仅 `10 + 3 = 13`）。Enzyme 把 **成本决策提升到整张依赖图（DAG）粒度**，并结合历史执行统计持续校准，同时为 Python UDF 和非确定性逻辑保留受控的安全回退路径，在生产集群上每天节省数十亿 CPU 秒。

### 4.3 Google BigQuery Incremental MV & Continuous Queries：严格可微子集 + Smart Tuning 读时拼装

Google BigQuery 则把“克制边界”做到了极致，并衍生出与 Snowflake/Databricks 截然不同的**查询时新鲜度保障机制**：

1. **严守紧凑可微子集，用 Smart Tuning 消灭后台刷新延迟**：BigQuery 的原生增量物化视图对 SQL 形状限制得相当严格——只接受过滤、内连接以及标准/近似可合并聚合（`SUM`、`COUNT`、`AVG`、`MIN`、`MAX`、`APPROX_COUNT_DISTINCT` 等），凡是 `OUTER JOIN`、`UNION ALL`、`HAVING`、窗口函数或复杂子查询一概不走原生增量。换来的巨大回报是**极简、零意外的后台增量维护与“读时强一致”**：即使后台刷新任务暂时落后了几分钟，当用户查询命中物化视图时，BigQuery 的 **Smart Tuning** 引擎会自动把“已持久化的旧 MV 缓存”与“基表尚未刷新的尾部增量”在线合并算出最新结果！如果业务确实需要包含 `OUTER JOIN` 或窗口函数的物化视图，必须显式打开 `OPTIONS(allow_non_incremental_definition = true)` 并强制配置 `max_staleness`——后台走定时全量刷新，一旦视图陈旧度超过阈值，查询自动回退扫基表。
2. **用独立的 Continuous Queries 承接真正低延迟流管道**：BigQuery 没有强迫增量物化视图去扛毫秒级事件流，而是单独推出了基于受限 SQL 子集与专用 Slot 预留池的 **Continuous Queries**，持续消费流式写入或 Pub/Sub 并做轻量实时转换落表，再由下游增量 MV 承接分析聚合——把“极低延迟单行处理”和“高吞吐增量聚合”彻底解耦。

---

## 五、开源 OLAP 阵营的演进分野：StarRocks、Apache Doris 与 Databend

视线转回开源分析型数据库与湖仓引擎。和三大云厂商从零控制封闭存储不同，开源 OLAP 引擎（StarRocks、Apache Doris、Databend）历史上都是为高并发批处理与实时写入而生的，它们的增量计算演进普遍经历了一条清晰的两阶段路线：**先利用分区剪枝做好“分区级增量刷新”，再逐步啃下“行级 IVM（Row-level Incremental View Maintenance）”这块硬骨头。**

### 5.1 StarRocks：从分区级刷新迈向基于 TVR 的湖仓行级 IVM

在 StarRocks 3.x 时代，异步物化视图（Async MV）的主力杀手锏是 **PCT（Partition Change Tracking，分区级增量刷新）**：通过维护基表分区与 MV 分区的映射关系，每次只对发生变更的分区执行 `INSERT OVERWRITE`，并在查询改写阶段用“新鲜 MV 分区 `UNION ALL` 过期基表分区”实现透明加速。这套机制在按天分区的数仓宽表上非常高效，可一旦单日分区里高达数亿行、每分钟却只有几千条订单更新，反复重写整天分区的放大税就彻底扛不住了。

因此，StarRocks 在 4.0 演进中引入了真正的**行级增量物化视图（Incremental MV）**，其源码设计（详见 [StarRocks 源码剖析]({{< relref "2026-08-09-starrocks-architecture-and-internals.md#mv-的两条独立链路刷新与查询改写" >}})）与学术界的 **TVR（Time-Varying Relations）** 一脉相承：
- **优化器侧**：新增专门的 `IvmRewriter`（取代早期分散的 `TVR_REWRITE_RULES`）以及基于 `VersionRange` 的 TVR 算子重写规则，将基表扫描转换为按版本区间读取增量，并将聚合改写为中间状态合并；
- **刷新执行器分层**：通过 `MVRefreshProcessorFactory` 按 `RefreshMode` 精确路由——`PCT` 走 `MVPCTRefreshProcessor`，行级增量 `INCREMENTAL` 走 `MVIVMRefreshProcessor`（当基表缺少 TVR 基线版本时自动切到 `MVHybridRefreshProcessor` 重建），`AUTO` 走混合处理器；
- **紧贴 Apache Iceberg 生态**：重点围绕 Iceberg 快照增量读取与 v3 Row Lineage 打通湖仓增量链路，让刷新开销随数据变化量而非全表历史规模线性增长；
- **一条清醒的架构解耦**：StarRocks 明确把“数据新鲜度（刷新链路）”和“查询加速（透明改写链路）”拆开治理——在已检视的代码基线中，`MvRewritePreprocessor` 对 `refresh_mode=INCREMENTAL` 或 `AUTO` 的 MV 显式拒绝透明查询改写，避免增量状态表的中间形态污染通用 CBO 的等价改写搜索空间。

### 5.2 Apache Doris：MTMV 的“三级阶梯”——成熟分区改写 + 演进中的 Table Stream 行级 IVM

Apache Doris 围绕 **MTMV（异步物化视图）** 走出了一条极具务实色彩的渐进路线，把刷新粒度整齐地切成了四档策略：
1. **主力基盘（`PARTITIONS` 分区增量 + 透明改写）**：在生产最广泛的 2.x/3.x 版本里，Doris 通过 `MTMVPartitionUtil`、`MTMVBaseVersions` 与 `MTMVRefreshPartitionSnapshot` 精准追踪基表（含内表与湖外表）的分区版本演化，只重算变更分区；同时在 Nereids 优化器里支持完整的透明查询改写与部分新鲜分区的 `UNION ALL` 基表补偿。
2. **迈向行级 IVM（`INCREMENTAL`）**：针对大分区内高频细粒度 DML 的痛点，Doris 社区在后续版本演进中（5.0 实验性链路）构建了端到端的原生增量栈——**`OLAP Table DML → Row Binlog → Table Stream → MTMV IVM`**。底层由 Row Binlog 与 Table Stream 承接精确的行级变更捕获，上层 MTMV IVM 支持投影、过滤、内/外/全连接、常规及 Bitmap 聚合与 `UNION ALL` 的行级增量维护。
3. **自适应降级（`REFRESH AUTO`）**：当配置为 `AUTO` 时，Doris 按 **`INCREMENTAL` → `PARTITIONS` → `COMPLETE`** 的顺序逐级探测与回退——能行级增量就绝不重算整分区，不满足行级约束就退守分区级增量，再不行才兜底全表重算。这种三级安全网非常契合混合负载下的生产运维预期。

### 5.3 Databend：基于 FUSE 对象存储的三对象分工与“读时混合修补”

作为云原生 Rust 列存数仓，Databend 基于不可变对象存储格式 **FUSE**，没有把所有增量语义硬塞进同一个对象，而是把职责清晰地切分给了三个互补原语：
- **Streams（CDC 层）**：零拷贝追踪基表 `INSERT`/`UPDATE`/`DELETE` 变更日志，配合 **Tasks** 与 `STREAM_STATUS` 守卫，供用户构建可控的命令式增量 ETL 管道；
- **Materialized Views（单表高频加速 + 读时修补）**：限定在单张 FUSE 基表的过滤与标准聚合上，创建时自动开启源表的 `CHANGE_TRACKING`。它的亮点和 BigQuery Smart Tuning 异曲同工——采用 **Hybrid Read-Fix（读时混合修补）** 机制：当查询撞见尚未后台刷新的源表新增块时，引擎在读取路径上实时合并已落盘的 MV 快照与未刷新增量，既保住了读强一致，又避免了每次微写都触发重型后台重写；
- **Dynamic Tables（多表声明式 ELT 落盘）**：用于承载包含多表 Join 和复杂聚合的衍生表物化到 FUSE 存储。与单表 MV 不同，Databend 现阶段的 Dynamic Table 主打清晰的快照隔离与声明式管理，核心通过显式 `REFRESH DYNAMIC TABLE` 执行全量刷新，返回最近一次提交的静态结果，而非在读路径上做多表在线修补。

把理论流派、两个个人实验、三大云数仓与开源阵营（含 Feldera）全部收进一张全景图谱与对照表里，整个增量计算世界的分工坐标就完全透亮了：

![从个人实验原型到云数仓与开源引擎：增量计算架构演进图谱](/images/diagrams/ivm-personal-demos-to-industry-spectrum.svg)

| 系统 / 实验 | 核心理论路线 | 变更捕获来源 | 算子状态归属 | 优化器与代价决策 | `Sort/Limit`/复杂 SQL 策略 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`optd-incremental-sql`**（个人实验） | 代数 \(\Delta\) 下推 + 8 级显式策略 | 内存事务 Delta / Iceberg 快照 | 内存 COW AVL 树 + 块级 Arrow Checkpoint | 确定性规则编译 + 严格 Preflight 预检 | 严格模式显式拒绝，不隐藏全量回退 |
| **`vela-dbsp`**（个人实验） | DBSP Z-Set 电路 | Iceberg v2 快照/文件 Diff（识别 COW 零变更） | 内存索引/懒删堆 + `vela-dbsp-trace-v1` 零重放恢复 | **真实 Cascades 三维代价模型**（初始/维护/状态） | 浮点聚合显式回退 `group_rescan`，全量对拍 DuckDB |
| **Feldera**（开源 Rust） | 原生 DBSP（VLDB '23） | Kafka / CDC / HTTP 流输入 | 持久化 LSM/Trace 存储（支持超内存溢写） | SQL 编译为增量数据流电路 | 完整 Z-Set 流式语义，面向持续低延迟计算 |
| **Snowflake Dynamic Tables** | DVS（延迟视图语义）+ IVM | 原生微分区 Change Tracking / Stream | 目标动态表微分区 + 内部隐式状态 | `AUTO` 自动判别增量/全量，支持 `FROZEN WHERE` 冻结历史 | 不支持构造自动或显式切回 `FULL` 刷新 |
| **Databricks Enzyme** | 湖仓 IVM + **DAG 全局代价优化** | Delta CDF / Row Tracking / Deletion Vectors | **顶层视图 + 底层 Delta 辅助状态表**（如 `SUM+COUNT`） | 跨多级 MV 依赖图全局成本寻优 + 历史反馈校准 | 支持安全回退至全量，兼容 Python UDF |
| **Google BigQuery** | 严格可微子集 + Smart Tuning | 托管存储变更历史 / 流式写入缓冲 | 托管 MV 缓存 + 查询时在线合并尾部增量 | 编译期严格白名单判定；流场景剥离至 Continuous Queries | 非增量需显式 `allow_non_incremental_definition` + `max_staleness` |
| **StarRocks** | PCT 分区追踪 → **TVR 行级 IVM** | 分区版本映射 / Iceberg Snapshot & Row Lineage | 物化视图表 + TVR 增量合并态 | `IvmRewriter` + `MVRefreshProcessorFactory` 路由 | 缺少基线切 `Hybrid` 重建；增量 MV 与透明改写解耦 |
| **Apache Doris** | MTMV 分区改写 → Row Binlog 行级 IVM | 分区版本快照 / `Row Binlog → Table Stream` | MTMV 表与增量状态 | `REFRESH AUTO` 按 `INCREMENTAL → PARTITIONS → COMPLETE` 三级降级 | 自动降级至分区刷新或全量刷新 |
| **Databend** | FUSE 三对象分工（Stream / MV / DT） | FUSE `CHANGE_TRACKING` / Stream | FUSE 不可变块（单表 MV 读时修补，DT 独立落盘） | 单表 MV 走 Hybrid Read-Fix；多表由 DT 显式刷新 | Dynamic Table 直接全量刷新，边界清晰 |

---

## 六、设计反思：如果今天给湖仓引擎补增量计算，边界该画在哪里？

从两份亲手写出来的实验代码一路走到今天工业界的云数仓和开源内核，最深的体会反而是一种“反直觉的克制”：**造一套能跑通几个单测的增量算子很容易，但造一套在生产环境里既不算错账、又不会把内存和对象存储账单撑爆的增量引擎，成败往往取决于五条边界怎么画。**

1. **先把上游 CDC 契约钉死，再谈下游增量算子**。任何试图绕开“稳定行身份（Row Lineage）”与“前像获取（Before-image）”直接做表级增量的设计，最后都会在 COW 文件重写和多次更新抵消上翻车。`vela-dbsp` 专门测试“COW 文件重写但逻辑净增量为 0”的场景，和 Iceberg v3 强制引入 `_row_id`、Delta 推 Row Tracking，解决的其实是同一道前置命题。
2. **线性算子绝不能留死状态，状态必须跟算子能力精准对齐**。`optd-incremental-sql` 在 TPC-H/TPC-DS 上测出 73% 的节点是无状态线性节点，这意味着增量引擎的编译器必须做**静态快照可达性分析**，只给真正需要历史状态（Join 索引、Match-Count 计数、Extrema 极值堆、可逆累加器）的节点分配状态槽，并且让多个衍生视图共享同一份基表快照。
3. **不要只做“单算子规则”，必须把“全量 vs 增量”放进全局代价空间**。无论是 `vela-dbsp` 用 Cascades 三维代价挑 `retractable_extrema` 还是 `group_rescan`，还是 Tempura 与 Databricks Enzyme 在 DAG 级别平衡单步最优与下游雪崩，都在印证同一件事：增量只是执行计划里的一个物理选项，而不是宗教信仰。当变更比例跨过拐点、或热点键扇出爆炸时，能优雅且全局最优地切换全量，比硬扛纯增量重要十倍。
4. **把状态持久化做成“增量块 + 可控回放”，而不是整树全量序列化**。常驻内存状态一旦脱离单机玩具规模，立刻撞上重启冷启动和 Checkpoint 尖刺两座大山。要么像 Databricks Enzyme 那样把中间状态直接存成列存 Delta 表借力湖仓底座，要么像 `optd-incremental-sql` 演进路线和 Feldera 那样采用分块增量落盘与可溢写有序 Trace，把 Checkpoint 写放大牢牢压在 \(O(|\Delta_{\text{keys}}|)\)。
5. **诚实的 Fail-Closed 护栏，比“假装支持全 SQL”值钱得多**。无论是 `optd` 的“未消解 `Δ` 立即报错”、`vela-dbsp` 对浮点聚合回撤的显式回退，还是 Snowflake 的 `FROZEN WHERE`、BigQuery 的 `max_staleness`、StarRocks 对增量刷新与透明改写的解耦——成熟的工业系统从不向用户承诺不存在的免费午餐，而是把语义边界、延迟契约与失败回退全部摆在明面上。

把增量计算放回整个数据基础设施的版图里看，它其实正在扮演“流批一体”最后一块真正可落地的拼图：不再要求工程师一边写离线 SQL、一边另起一套复杂的流计算状态集群，而是用同一套声明式关系代数，沿着延迟光谱从毫秒、秒、分钟到小时平滑滑动。而支撑这块拼图站稳脚跟的，归根结底还是那几条朴素的底层规律——**代数可微性、状态最小化、全局代价账本，以及永不妥协的正确性契约**。

---

## 参考资料与源码基线

### 个人实验源码基线
- **`optd-incremental` / `optd-incremental-sql`**：基于 `optd` 与 DataFusion 的 Rust 增量维护实验（本地基线 `f201a6e`，核心模块：`optd-incremental/src/rules.rs`、`optd-incremental-sql/src/planner/incremental_optimizer.rs`、`optd-incremental-sql/src/state_directory.rs`，状态复盘见 `docs/src/incremental/rfc_state_management.md`）。
- **`vela-dbsp`**：基于 Python、Cascades 优化器、PyIceberg v2 与 DuckDB 1.1.3 对拍的 DBSP 增量实验（本地基线 `b4a2d7a`，核心模块：`dbsp/core/ivm.py`、`dbsp/engine/aggregate_nodes.py`、`dbsp/planner/cost_model.py`、`dbsp/verify/checkpoint.py`）。

### 学术论文与理论文献
- Mihai Budiu et al., *DBSP: Automatic Incremental View Maintenance for Rich Query Languages*, [PVLDB 2023 (Best Paper)](https://arxiv.org/abs/2203.16684) · 开源工业实现：[Feldera](https://github.com/feldera/feldera)（参考基线 `dbb8fba`）。
- Yanif Ahmad et al., *DBToaster: Higher-order Delta Processing for Dynamic, Frequently Fresh Views*, [PVLDB 2012](https://vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf).
- Frank McSherry et al., *Differential Dataflow*, [CIDR 2013](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper111.pdf).
- Zuozhi Wang et al., *Tempura: A General Cost-Based Optimizer Framework for Incremental Data Processing*, [PVLDB 2020/2021](https://arxiv.org/abs/2009.13631).
- Edmon Begoli et al., *One SQL to Rule Them All — an Efficient and Syntactically Idiomatic Approach to Management of Streams and Tables*, [SIGMOD 2019](https://arxiv.org/abs/1905.12133).
- *The Role of Semirings in Incremental View Maintenance*, [arXiv 2026](https://arxiv.org/abs/2606.07795v2).

### 工业界与开源系统公开文档
- **Snowflake Dynamic Tables**：[Dynamic Tables Overview & Refresh Modes](https://docs.snowflake.com/en/user-guide/dynamic-tables-about)、*What's the Difference? Incremental Processing with Change Queries in Snowflake*（[PACMMOD 2023](https://dl.acm.org/doi/10.1145/3589776)）及 *Delayed View Semantics and Snowflake Dynamic Tables*。
- **Databricks Enzyme**：*Enzyme: Incremental View Maintenance With Spark Declarative Pipelines*（[SIGMOD 2026 Industry / arXiv](https://arxiv.org/abs/2603.27775)）。
- **Google BigQuery**：[Materialized Views Overview & Incremental Refresh](https://cloud.google.com/bigquery/docs/materialized-views-intro)、[Continuous Queries](https://cloud.google.com/bigquery/docs/continuous-queries-introduction)。
- **StarRocks**：[Asynchronous Materialized Views](https://docs.starrocks.io/docs/using_starrocks/async_mv/Materialized_view/)（文中 `IvmRewriter`、`MVIVMRefreshProcessor` 等类结构对应本博客已核验源码基线 `f977b53`）。
- **Apache Doris**：[Asynchronous Materialized Views (MTMV) & Incremental Refresh](https://doris.apache.org/docs/query-acceleration/materialized-view/async-materialized-view/overview/)（本地检出参考 `5ff9558` 及社区 5.0 IVM 演进文档）。
- **Databend**：[Streams](https://docs.databend.com/sql/sql-commands/ddl/stream/)、[Materialized Views](https://docs.databend.com/sql/sql-commands/ddl/view/) 与 [Dynamic Tables](https://docs.databend.com/sql/sql-commands/ddl/dynamic-table/) 官方文档。

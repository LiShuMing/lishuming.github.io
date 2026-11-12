---
title: "【Paper】数据库经典论文精读总纲：从关系代数到分布式共识的 67 篇演进地图"
date: 2026-10-22T14:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "database-papers-source-reading"
categories: ["数据库"]
tags: ["论文精读", "数据库系统", "查询优化器", "存储引擎", "分布式共识"]
series: ["数据库经典论文精读"]
description: "以第一性原理串联数据库领域 50 年间最具代表性的 67 篇经典文献：拆解语义与物理存储解耦、组合搜索剪枝、CPU/内存层次匹配、RUM 空间放大权衡与异步网络状态复制五大核心张力，并提供 10 篇专题长文与原始 PDF 的完整阅读导航。"
draft: false
toc: true
math: true
---

如果你把过去五十年间的数据库经典论文摊在桌面上逐篇孤立阅读，很容易陷入一种“技巧博物馆”式的疲惫：1970 年代在争论关系演算与块结构语法，1990 年代在推演 Cascades Memo 与直方图桶边界，2000 年代转向向量化批次与缓存行预取，2010 年代以后又涌现出学习型基数估计、指针混洗（Pointer Swizzling）缓冲池与弱化多数派（Quorum）共识。表面上看，每篇论文都在发明一套独立的术语、代价公式与基准测试；但如果退后一步追问——**如果我们用最朴素的方式实现一个共享数据系统，最先撞上哪几堵物理或数学高墙？**——这 67 篇文献立刻就会收敛成五条清晰的演化主轴。

## 五大底层张力：为什么数据库内核长成今天这样

设想我们要从零写一套支持并发读写的 SQL 数据库。最直觉的做法是：把记录按结构体顺序追加进磁盘文件，收到查询后按语法树嵌套循环扫描记录，遇到并发修改就加一把全局互斥锁，需要容灾时把文件同步复制到另一台机器。这个玩具原型在几十行数据上完全正确，但只要数据量、查询复杂度或机器规模跨过数量级，五个根本矛盾就会依次引爆：

1. **声明式语义与物理布局的绑定之痛（关系模型与系统架构）**：如果应用程序代码里写死了指针跳转路径或索引名字，一旦底层为了性能把行存改成列存、或者增删一个辅助索引，上层业务代码就会全部作废。Codd 在 1970 年提出关系模型的初衷，就是用集合代数在“用户声明要什么”与“引擎决定怎么取”之间劈开一道数据独立性（Data Independence）防火墙；而此后 INGRES、SEQUEL、Postgres 乃至现代 DBMS 内核的进程模型、缓冲管理与目录演化，都是在回答如何在工程上兑现这道防火墙，同时处理重复行（Bag 语义）与三值逻辑（`NULL`）带来的代数破缺。
2. **指数级搜索空间与不完美分布假设的对撞（查询优化、去相关与基数估计）**：一旦把物理访问路径交给系统自动选择，\(N\) 张表连接就会产生阶乘级的连接顺序与物理算子组合；更棘手的是，SQL 允许业务写出任意深度的关联子查询（Correlated Subquery）与外连接（Outer Join），如果直接按语法嵌套求值，内层表会被反复扫描数百万次，而盲目重排又会踩中 `COUNT(*)` 空集变 `1` 或 `NULL` 谓词穿透的语义陷阱。优化器不仅需要 Volcano/Cascades 与函数依赖（Functional Dependency）把等价搜索空间压缩进多项式级 Memo，还必须在极小的内存预算内用直方图、小波、Sketch 或自回归模型逼近真实的多列联合分布，否则算错一个数量级的基数（Cardinality），就会选出慢上百倍的执行计划。
3. **指令流水线停顿与内存墙延迟的拉锯（向量化、编译执行与多核并行）**：即便优化器选出了完美的物理计划，传统的 Volcano `next()` 虚函数逐行迭代也会在现代超标量 CPU 上撞墙——算子框架每吐出一行元组就要经历十几次跨函数跳转，分支预测器被彻底打乱，CPU 90% 的时钟周期都在空等指令缓存与 LLC/DRAM 未命中。MonetDB/X100 的列式小批量向量化（Vectorization）、HyPer 的 LLVM JIT 算子融合（Operator Fusion）、ROF 的软件预取阶段切分，以及 Morsel-Driven 的弹性 NUMA 调度，都是在围绕寄存器局部性、SIMD 吞吐与编译延迟寻找新的平衡点。
4. **读放大、写放大与内存开销的不可能三角（存储引擎与缓冲管理）**：下沉到持久化层后，介质物理特性决定了没有任何一种索引结构能同时做到点查最快、随机写最快且空间占用最小——这正是 RUM 猜想揭示的铁律。B+ 树选择原地更新以保住读性能，代价是随机写放大；LSM-Tree 用顺序追加与后台归并（Compaction）吞吐海量写入，却立刻面临读路径跨多层 SSTable 查找的惩罚，进而催生了 Fractal Tree、SuRF 范围过滤器、Bourbon 学习索引与存算分离压实（CaaS-LSM）；而在 NVMe SSD 带宽逼近内存总线的今天，传统缓冲池的全局哈希表锁又成了新瓶颈，迫使 LeanStore 与 Umbra 用指针混洗和可变尺寸页重构缓冲管理。
5. **多核争用与异步不可靠网络下的状态一致性（并发控制与分布式共识）**：当并发工作线程从几十核扩展到上千核，单点时间戳分配器与锁表互斥本身就会把吞吐拖垮；而一旦数据跨越多台随时可能丢包、乱序甚至停机（Crash-Recovery）的物理节点，物理时钟偏斜使得“谁先谁后”失去全局基准。从 Adya 基于依赖图（Dependency Serialization Graph）重写隔离级别定义，到 Percolator、Megastore、HLC 与 Anna 在跨分片协调与无锁格合并（Lattice）之间划清界限，再到 Paxos 泛化、Flexible/Generalized Quorum 弱化与 Raft/Ark 工程落地，分布式层面的所有协议都在精确回答同一个问题：**在每一次正常读写与每一次故障切换时，究竟最少需要多少次跨网络协调，才能守住不丢数据、不读脏数据的底线？**

![数据库经典论文 10 大演进脉络全景图](/images/diagrams/paper-db-system-map.svg)

## 十篇专题精读阅读路线

为了把散落在不同年代与顶会里的 67 篇论文还原为有因果脉络的技术演进史，本系列按上述五大底层张力重组为 **10 篇专题深度长文**。每篇长文内部先从第一性原理推演该方向的核心工程矛盾与演化主线，再按具体编号（`#01`–`#67`）逐篇拆解每篇论文的真实问题边界、核心算法机制、实验工作负载假设与工业界落地取舍，并附带固定版本的原始 PDF 页码索引：

| 篇章 | 专题文章 | 覆盖层次 | 收录论文编号（共 67 篇） | 核心工程问题 |
|---:|---|---|---|---|
| **01** | [从关系模型到数据库内核架构：Codd、INGRES、SEQUEL、Postgres 与现代 DBMS 全局解剖]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}) | 模型与架构基石 | `#41` `#42` `#43` `#44` `#40` `#53` `#29` | 为什么需要数据独立性？SQL 的重复集与 `NULL` 留下了哪些代数隐患？现代 DBMS 的进程、内存与存储子系统如何咬合？ |
| **02** | [查询优化器演进精读：Volcano/Cascades 搜索框架、函数依赖、属性传递与真实 Join 代价]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}) | 优化器搜索与属性 | `#03` `#01` `#02` `#12` `#13` `#14` `#06` | 如何把自顶向下规则搜索、排序/分区物理属性推导（Enforcer）与函数依赖放进同一个可剪枝框架？为什么教科书代价公式经常报错？ |
| **03** | [外连接重排与子查询去相关演进：从 COUNT 空集陷阱、EELs 到 HyPer 任意嵌套解耦]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}) | 等价改写与去相关 | `#04` `#05` `#15` `#16` `#17` `#19` `#18` | 外连接为什么破坏结合律？`COUNT` 空集 Bug 是怎么发生的？HyPer 如何利用 Dependent Join 下推彻底消灭任意关联子查询？ |
| **04** | [数据摘要与概率计数精读：直方图、小波、FM/PCSA、Count-Min Sketch 与 Distinct Sampling]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}) | 离线与流式摘要 | `#10` `#11` `#09` `#52` `#47` `#49` | 在几 KB 内存预算内，如何刻画倾斜列分布、高频项（Heavy Hitters）与带过滤条件的基数（NDV），并给出严格的误差上下界？ |
| **05** | [从执行反馈到深度基数估计：DB2 LEO/ASC、Oracle 自动调优、AQP 与 Naru/NeuroCard 演进]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}) | 自适应与学习估计 | `#08` `#50` `#07` `#38` `#39` `#48` `#51` | 列独立性假设失效后，用执行反馈修补目录、运行期切换计划（AQP）与深度自回归模型学习多表联合分布各自卡在什么成本边界上？ |
| **06** | [查询执行范式演进精读：从 Volcano 迭代器、MonetDB/X100 向量化到松弛算子融合与自适应编译]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}) | 单核执行与编译 | `#28` `#26` `#24` `#21` `#27` `#20` | 向量化与 JIT 编译究竟各自省掉了哪部分 CPU 开销？面对大哈希表 LLC 未命中与短查询编译耗时，ROF 与自适应执行如何破局？ |
| **07** | [多核并行、主存连接与数据表示精读：Morsel 调度、硬件哈希连接、LIP、开放列存与高速网络]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}) | 多核、列存与网络 | `#25` `#23` `#22` `#45` `#46` | 众核 NUMA 上如何消灭静态分区倾斜？分区哈希与无分区哈希之争的真实瓶颈在哪？Parquet/ORC 编码与 RDMA 网络如何重塑分析引擎？ |
| **08** | [存储结构与缓冲管理演进精读：五分钟法则、RUM 猜想、LSM 家族与 LeanStore/Umbra]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}) | 存储引擎与缓冲池 | `#55` `#58` `#63` `#61` `#56` `#62` `#59` `#57` `#60` `#64` | 介质经济学如何决定缓存边界？LSM 家族如何缓解读写与空间放大？NVMe 时代 LeanStore 与 Umbra 如何绕开传统 Buffer Pool 锁开销？ |
| **09** | [事务隔离、千核并发与分布式存储精读：Adya 隔离模型、Abyss、Percolator、Megastore、HLC、PACELC 与 Anna]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}) | 并发控制与事务 | `#65` `#67` `#66` `#54` `#37` `#36` `#34` | ANSI SQL 隔离级别定义错在哪？千核下七种并发控制为何集体崩盘？跨节点事务与因果快照如何在延迟、一致性与协调成本之间取舍？ |
| **10** | [分布式共识协议演进精读：Paxos 泛化与 Quorum 弱化、Generalized Consensus、Raft 证明边界与 Ark 工程实践]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}) | 共识协议与复制 | `#32` `#30` `#33` `#31` `#35` | Classic Paxos 的两阶段多数派交集到底哪些可以缩减、哪些可以通过可交换命令（Commutativity）绕开？Raft 强主设计与真实生产实现需要注意哪些边界？ |

如果你是第一次系统梳理数据库内核文献，建议先通读 **专题 01** 建立全局模块坐标，随后根据自己的工程重心选择分支：
- **深耕查询优化器（Optimizer）**：按 **专题 02 → 专题 03 → 专题 04 → 专题 05** 顺序推进，这条线完整覆盖了从 Cascades 搜索引擎、子查询去相关到统计直方图与学习型基数估计的全栈脉络。
- **深耕执行引擎与分析型内核（OLAP Execution）**：按 **专题 06 → 专题 07** 阅读，重点体会 CPU 微架构（IPC、分支预测、SIMD、TLB、NUMA）与列存压缩格式、RDMA 网络之间的联动。
- **深耕存储引擎与分布式事务（Storage & Distributed Systems）**：按 **专题 08 → 专题 09 → 专题 10** 推进，把单机页面组织、缓冲池并发、MVCC 依赖图与跨机状态机复制连成一体。

---

## 67 篇论文全量分类检索表

下表按 10 大专题列出全部 67 篇论文的精读锚点与原始 PDF 链接，点击**精读章节**可直接跳转至对应专题长文内的单篇深度拆解（`#paper-NN`）：

### 一、关系模型、SQL 语义演进与 DBMS 架构（7 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#41` | *A Relational Model of Data for Large Shared Data Banks*（Codd 1970：数据独立性、关系代数与一致性边界） | 11 | [专题 01 · `#paper-41`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-41) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-relational-model-of-data-for-large-shared-data-banks.pdf) |
| `#42` | *Extending the Database Relational Model to Capture More Meaning*（RM/T：代理键 E-domain、实体完整性与图代数） | 38 | [专题 01 · `#paper-42`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-42) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/extending-the-database-relational-model-to-capture-more-meaning.pdf) |
| `#43` | *INGRES: A Relational Data Base System*（QUEL 演算、元组替换查询分解 One-Variable Detachment 与视图改写） | 8 | [专题 01 · `#paper-43`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-43) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/ingres%3A-a-relational-data-base-system.pdf) |
| `#44` | *SEQUEL: A Structured English Query Language*（块结构映射、Bag 重复行保留与 `GROUP BY` 量化设计） | 16 | [专题 01 · `#paper-44`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-44) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/sequel%3A-a-structured-english-query-language.pdf) |
| `#40` | *A Critique of the SQL Database Language*（C. J. Date 1983：非正交语法、重复语义与 `NULL` 三值逻辑改写陷阱） | 47 | [专题 01 · `#paper-40`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-40) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-critique-of-the-sql-database-language.pdf) |
| `#53` | *Looking Back at Postgres*（Stonebraker 复盘：ADT 可扩展类型成功路线 vs 规则系统与无日志时间旅行存储教训） | 9 | [专题 01 · `#paper-53`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-53) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/looking-back-at-postgres.pdf) |
| `#29` | *Architecture of a Database System*（Hellerstein 等：进程/线程调度、查询处理、事务锁日志与存储缓冲全景解剖） | 119 | [专题 01 · `#paper-29`]({{< relref "2026-10-22-db-papers-relational-model-and-architecture.md" >}}#paper-29) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/architecture/architecture-of-a-database-system.-foundations-and-trends-in-databases.pdf) |

### 二、查询优化器框架、物理属性与代价模型（7 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#03` | *The Volcano Optimizer Generator: Extensibility and Efficient Search*（逻辑/物理代数分离、物理属性向量与分支界限剪枝） | 10 | [专题 02 · `#paper-03`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-03) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-volcano-optimizer-generator--extensibility-and-efficient-search.pdf) |
| `#01` | *The Cascades Framework for Query Optimization*（任务驱动按需探索、Memo 共享、Guidance 规则控制与精确剪枝） | 10 | [专题 02 · `#paper-01`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-01) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/the-cascades-framework-for-query-optimization.pdf) |
| `#02` | *Parallelizing Query Optimization on Shared-Nothing Architectures*（多核/分布式 DP 子集搜索切分与通信规避） | 12 | [专题 02 · `#paper-02`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-02) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/optimizer-framework/parallelizing-query-optimization-on-shared-nothing-architectures.pdf) |
| `#12` | *Exploiting Functional Dependence in Query Optimization*（Glenn Paulley 博士论文：FD 严格/松散等价推导、消除冗余 `DISTINCT`/`GROUP BY`） | 334 | [专题 02 · `#paper-12`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-12) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/%5Bthesis%5D-exploiting-functional-dependence-in-query-optimization.pdf) |
| `#13` | *An Efficient Framework for Order Optimization*（Neumann & Moerkotte：用有限状态自动机 FSM 紧凑编码排序属性与 FD 等价类） | 24 | [专题 02 · `#paper-13`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-13) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/an-efficient-framework-for-order-optimization.pdf) |
| `#14` | *Incorporating Partitioning and Parallel Plans into the SCOPE Optimizer*（Cascades 框架内的结构化数据分布属性推导与 Exchange Enforcer） | 12 | [专题 02 · `#paper-14`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-14) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/functional-dependencies/incorporating-partitioning-and-parallel-plans-into-the-scope-optimizer.pdf) |
| `#06` | *Seeking the Truth About Ad Hoc Join Costs*（Haas 等：缓冲池驻留、顺序与随机 I/O 真实比例对 Hash/Sort-Merge Join 拐点的颠覆） | 27 | [专题 02 · `#paper-06`]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}}#paper-06) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/cost-model/seeking-the-truth-about-ad-hoc-join-costs.pdf) |

### 三、外连接重排与嵌套子查询去相关（7 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#04` | *Extending the Algebraic Framework of Query Processing to Handle Outerjoins*（Rosenthal & Reiner 1984：外连接交换/结合律失效条件与广义外连接） | 10 | [专题 03 · `#paper-04`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-04) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/extending-the-algebraic-framework-of-query-processing-to-handle-outerjoins.pdf) |
| `#05` | *Using EELs, a Practical Approach to Outerjoin and Antijoin Reordering*（Eligibility Lists 编码外连接与反连接合法重排约束） | 18 | [专题 03 · `#paper-05`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-05) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/join-order/using-eels%2C-a-practical-approach-to-outerjoin-and-antijoin-reordering.pdf) |
| `#15` | *Of Nests and Trees: A Unified Approach to Processing Queries That Contain Nested Subqueries, Aggregates, and Quantifiers*（Dayal 1987：广义聚合与外连接消除 `COUNT` 空集 Bug） | 12 | [专题 03 · `#paper-15`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-15) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/a-unitied-approach-to-processing-queries-that-contain-nested-subqueries%2C-aggregates%2C-and-quantifiers.pdf) |
| `#16` | *Optimization of Nested Queries in a Distributed Relational Database*（R\* 分布式关联子查询求值时机、缓存去重与通信代价） | 13 | [专题 03 · `#paper-16`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-16) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/optimization-of-nested-queries-in-a-distributed-relational-database.pdf) |
| `#17` | *SQL-like and QUEL-like Correlation Queries with Aggregates Revisited*（Kiessling：多级关联聚合的双重 `COUNT` 空集反例与 QUEL 域去重准则） | 17 | [专题 03 · `#paper-17`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-17) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/sql-like-and-quel-like-correlation-queries-with-aggregates-revisited.pdf) |
| `#19` | *Unnesting Arbitrary Queries*（Neumann & Kemper 2015：Dependent Join 代数下推法则彻底解耦任意嵌套 SQL） | 20 | [专题 03 · `#paper-19`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-19) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/unnesting-arbitrary-queries.pdf) |
| `#18` | *The Complete Story of Joins (in HyPer)*（Mark Join 处理三值逻辑 `IN`/`ANY`、Single Join 与非等值物理连接实现） | 20 | [专题 03 · `#paper-18`]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}#paper-18) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/nested-query/the-complete-story-of-joins.pdf) |

### 四、离线数据摘要、直方图与流式概率计数（6 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#10` | *The History of Histograms*（Ioannidis：等宽、等深、V-Optimal 与 MaxDiff 直方图分类学演进） | 12 | [专题 04 · `#paper-10`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-10) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/the-history-of-histograms.pdf) |
| `#11` | *Universality of Serial Histograms*（序列直方图在自连接与等值连接误差最小化上的理论最优性边界） | 12 | [专题 04 · `#paper-11`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-11) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/universality-of-serial-histograms.pdf) |
| `#09` | *Synopses for Massive Data: Samples, Histograms, Wavelets, Sketches*（Cormode 等长篇综述：四大摘要结构的误差契约与更新复杂度对比） | 296 | [专题 04 · `#paper-09`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-09) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/synopses-for-massive-data%3A-samples%2C-histograms%2C-wavelets%2C-sketches.pdf) |
| `#52` | *Probabilistic Counting Algorithms for Data Base Applications*（Flajolet & Martin 1985：首零位图 FM 与随机分桶平均 PCSA 奠定 HLL 前身） | 28 | [专题 04 · `#paper-52`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-52) | [PDF](https://algo.inria.fr/flajolet/Publications/FlMa85.pdf) |
| `#47` | *An Improved Data Stream Summary: The Count-Min Sketch and its Applications*（单向过估碰撞噪声、\((\varepsilon, \delta)\) 加性界与分位数/高频项提取） | 18 | [专题 04 · `#paper-47`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-47) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/an-improved-data-stream-summary%3A-the-count-min-sketch-and-its-applications%2C-journal-of-algorithms.pdf) |
| `#49` | *Distinct Sampling for Highly-Accurate Answers to Distinct Values Queries and Event Reports*（Gibbons：按哈希值层级自适应淘汰的值级一致抽样，破解带谓词 NDV 估计） | 10 | [专题 04 · `#paper-49`]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}#paper-49) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/distinct-sampling-for-highly-accurate-answers-to-distinct-values-queries-and-event-reports.pdf) |

### 五、执行反馈、自动诊断调优与深度基数估计（7 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#08` | *Automated Statistics Collection in DB2 UDB*（基于工作负载谓词分析、UDI 变更追踪与后台限流采样的统计信息闭环） | 12 | [专题 05 · `#paper-08`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-08) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/automated-statistics-collection-in-db2-udb.pdf) |
| `#50` | *LEO – DB2's LEarning Optimizer*（算子运行期真实基数与估计值比对、Adjustments 乘子目录修正与一致性冲突边界） | 10 | [专题 05 · `#paper-50`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-50) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/leo-%E2%80%93-db2%E2%80%99s-learning-optimizer.pdf) |
| `#07` | *Adaptive Query Processing in the Looking Glass*（Babu & Bizarro：Plan-based、Routing-based 与 Tuple-based AQP 机制与状态物化代价剖析） | 12 | [专题 05 · `#paper-07`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-07) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/adaptive-query-processing-in-the-looking-glass.pdf) |
| `#38` | *Automatic Performance Diagnosis and Tuning in Oracle*（ADDM 统一 DB Time 货币记账与等待事件因果分类树） | 11 | [专题 05 · `#paper-38`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-38) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-performance-diagnosis-and-tuning-in-oracle.pdf) |
| `#39` | *Automatic SQL Tuning in Oracle 10g*（离线时间预算内的局部探测求值、SQL Profile 辅助统计补丁与不改 SQL 文本的稳健调优） | 12 | [专题 05 · `#paper-39`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-39) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-sql-tuning-in-oracle-10g.pdf) |
| `#48` | *Deep Unsupervised Cardinality Estimation*（Naru：无监督深度自回归乘积法则建模单表联合分布与渐进重要性采样处理范围谓词） | 14 | [专题 05 · `#paper-48`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-48) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/deep-unsupervised-cardinality-estimation.pdf) |
| `#51` | *NeuroCard: One Cardinality Estimator for All Tables*（全外连接无偏采样、扇出缩放因子 Fanout Scaling 覆盖任意多表连接子图） | 13 | [专题 05 · `#paper-51`]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}#paper-51) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/neurocard%3A-one-cardinality-estimator-for-all-tables.pdf) |

### 六、查询执行范式：迭代器、向量化、JIT 编译与预取融合（6 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#28` | *Volcano — An Extensible and Parallel Query Evaluation System*（`open/next/close` 拉模型迭代器与 Exchange 元算子解耦数据流并行） | 16 | [专题 06 · `#paper-28`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-28) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/volcano---an-extensible-and-parallel-query-evaluation-system.pdf) |
| `#26` | *Query Evaluation Techniques for Large Databases*（Graefe 经典综述：外存排序、哈希分区、流水线双重缓冲与倾斜治理） | 98 | [专题 06 · `#paper-26`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-26) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/query-evaluation-techniquesfor-large-databases.pdf) |
| `#24` | *MonetDB/X100: Hyper-Pipelining Query Execution*（突破全列物化带宽墙，驻留 L1/L2 Cache 的小批量列向量执行引擎） | 14 | [专题 06 · `#paper-24`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-24) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/monetdn-x100-hyper-pipelining-query-execution.pdf) |
| `#21` | *Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask*（Kersten 等：同框架硬件计数器对决，拆解计算密集与内存受限场景的真实胜负手） | 14 | [专题 06 · `#paper-21`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-21) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/everything-you-always-wanted-to-know-about-compiled-and-cectorized-queries-but-were-afraid-to-ask.pdf) |
| `#27` | *Relaxed Operator Fusion for In-Memory Databases*（ROF：在编译流水线边界故意插入微批次暂存，同时解锁 SIMD 与群组软件预取） | 13 | [专题 06 · `#paper-27`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-27) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/relaxed-operator-fusion-for-in-memory-databases-making-compilation-vectorization-and-prefetching-work-together-at-last.pdf) |
| `#20` | *Adaptive Execution of Compiled Queries*（Kohn 等：基于 LLVM IR 字节码虚拟机零延迟起步，按代码块热度在线平滑热替换至机器码） | 12 | [专题 06 · `#paper-20`]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}#paper-20) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/adaptive-execution-of-compiled-queries.pdf) |

### 七、众核并行调度、主存连接、开放列存与高速网络（5 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#25` | *Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age*（弹性小数据块任务窃取、NUMA 本地写入与无锁哈希表合并） | 12 | [专题 07 · `#paper-25`]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}#paper-25) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/morsel-driven-parallelism-a-numa-aware-query-evaluation-framework-for-the-many-core-age.pdf) |
| `#23` | *Main-Memory Hash Joins on Modern Processor Architectures*（Balkesen 等：Radix 分区对抗 TLB 未命中、软件写合并缓冲区 SWWC 与无分区哈希对比） | 14 | [专题 07 · `#paper-23`]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}#paper-23) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/main-memory-hash-joins-on-modern-processor-architectures.pdf) |
| `#22` | *Looking Ahead Makes Query Plans Robust*（LIP：星型模式连接前向探测一组极低开销 Bloom Filter，消除中间结果爆炸） | 12 | [专题 07 · `#paper-22`]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}#paper-22) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/looking-ahead%20-makes-query-plans-robust.pdf) |
| `#45` | *A Deep Dive into Common Open Formats for Analytical DBMSs*（Parquet 与 ORC 在真实高基数字符串、嵌套列、字典退化与下推过滤上的硬核实测） | 13 | [专题 07 · `#paper-45`]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}#paper-45) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/formats/a-deep-dive-into-common-open-formats-for-analytical-dbmss.pdf) |
| `#46` | *The End of Slow Networks: It's Time for a Redesign*（Binnig 等：InfiniBand/RDMA 带宽反超单核内存拷贝后，分布式 Shuffle、Join 与 2PC 协议的重构） | 12 | [专题 07 · `#paper-46`]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}#paper-46) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/network/the-end-of-slow-networks%3A-it%27s-time-for-a-redesign.pdf) |

### 八、存储层级法则、LSM-Tree 演进家族与现代缓冲池（10 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#55` | *The Five-Minute Rule Thirty Years Later and Its Impact on the Storage Hierarchy*（DRAM/Flash/HDD IOPS 与容量单价剪刀差如何移动缓存驻留周期阈值） | 29 | [专题 08 · `#paper-55`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-55) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-media/the-five-minute-rule-thirty-years-later-and-its-impact-on-the-storage-hierarchy.pdf) |
| `#58` | *Designing Access Methods: The RUM Conjecture*（读开销 R、更新开销 U 与内存/空间开销 M 三者最多同时优化其二的统一分析坐标系） | 6 | [专题 08 · `#paper-58`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-58) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/designing-access-methods%3A-the-rum-conjecture.pdf) |
| `#63` | *The Log-Structured Merge-Tree (LSM-Tree)*（O'Neil 等 1996 奠基作：内存 \(C_0\) 与磁盘 \(C_1\) Rolling Merge 及最优相邻层容量比推导） | 32 | [专题 08 · `#paper-63`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-63) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/the-log-structured-merge-tree-%28lsm-tree%29.pdf) |
| `#61` | *LSM-based Storage Techniques: A Survey*（Luo & Carey：Leveled vs Tiered 压实、Monkey 非均匀内存分配与二级索引维护全景） | 25 | [专题 08 · `#paper-61`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-61) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/lsm-based-storage-techniques%3A-a-survey.pdf) |
| `#56` | *A Comparison of Fractal Trees to Log-Structured Merge (LSM) Trees*（\(B^\varepsilon\)-Tree 内部节点消息缓冲区与 LSM 尾延迟、范围扫描对比） | 15 | [专题 08 · `#paper-56`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-56) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/a-comparison-of-fractal-trees-to-log-structured-merge-%28lsm%29-trees.pdf) |
| `#62` | *SuRF: Practical Range Query Filtering with Fast Succinct Tries*（LOUDS-Dense/Sparse 压缩前缀树支撑范围存在性过滤与假阳性权衡） | 14 | [专题 08 · `#paper-62`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-62) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/surf-practical-range-query-filtering-with-fast-succinct-tries.pdf) |
| `#59` | *From WiscKey to Bourbon: A Learned Index for Log-Structured Merge Trees*（利用不可变 SSTable 特性训练分段线性回归模型，以及短寿命文件训练回本判断） | 18 | [专题 08 · `#paper-59`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-59) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/from-wisckey-to-bourbon%3A-a-learned-index-for-log-structured-merge-trees.pdf) |
| `#57` | *CaaS-LSM: Compaction-as-a-Service for LSM-based Key-Value Stores*（存算分离架构下远程无状态压实卸载、本地/远端混合调度与故障秒级回退） | 28 | [专题 08 · `#paper-57`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-57) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/caas-lsm%3A-compaction-as-a-service-for-lsm-based-key-value-stores-in-storage-disaggregated-infrastructure.pdf) |
| `#60` | *LeanStore: In-Memory Data Management Beyond Main Memory*（Leis 等：消除全局页表查询的父子指针混洗 Pointer Swizzling、Optimistic Lock Coupling 与冷却淘汰） | 12 | [专题 08 · `#paper-60`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-60) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/leanstore%3A-in-memory-data-management-beyond-main-memory.pdf) |
| `#64` | *Umbra: A Disk-Based System with In-Memory Performance*（`mmap` 虚拟地址管理可变尺寸大页、`German Strings` 12B 内联前缀字符串与 JIT 分析整合） | 7 | [专题 08 · `#paper-64`]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}#paper-64) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/umbra%3A-a-disk-based-system-with-in-memory-performance.pdf) |

### 九、事务隔离理论、千核并发控制与分布式存储权衡（7 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#65` | *Generalized Isolation Level Definitions*（Adya 等：抛弃基于锁的实现绑定，用读写/写读/反依赖有向图与谓词读定义 PL-1 至 PL-3 及 SI） | 12 | [专题 09 · `#paper-65`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-65) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/generalized-isolation-level-definitions.pdf) |
| `#67` | *Staring into the Abyss: An Evaluation of Concurrency Control with One Thousand Cores*（Yu 等：1000 核模拟器下 2PL、OCC、MVCC、H-Store 与 Calvin 各自的集中式扩展天花板） | 13 | [专题 09 · `#paper-67`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-67) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/staring-into-the-abyss%3A-an-evaluation-of-concurrency-control-with-one-thousand-cores.pdf) |
| `#66` | *Large-scale Incremental Processing Using Distributed Transactions and Notifications*（Percolator：基于 Bigtable 单行原子的主从锁 Primary Lock 两阶段提交与增量观察者） | 14 | [专题 09 · `#paper-66`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-66) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/large-scale-incremental-processing-using-distributed-transactions-and-notifications.pdf) |
| `#54` | *Megastore: Providing Scalable, Highly Available Storage for Interactive Services*（Entity Group 分区级无主快速写 Paxos、Coordinator 本地强读屏障与跨组 2PC/消息队列） | 12 | [专题 09 · `#paper-54`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-54) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/megastore%3A-providing-scalable%2C-highly-available-storage-for-interactive-services.pdf) |
| `#37` | *Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases*（HLC：无需原子钟硬件，在 NTP 误差界内编码 \((l, c)\) 捕获因果序并支持无阻塞全局快照读） | 13 | [专题 09 · `#paper-37`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-37) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/logical-physical-clocks-and-consistent-snapshots-in-globally-distributed-databases.pdf) |
| `#36` | *Consistency Tradeoffs in Modern Distributed Database System Design*（Abadi PACELC：区分网络分区时的 A/C 取舍与无故障常态复制路径上的延迟 L/一致性 C 权衡） | 6 | [专题 09 · `#paper-36`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-36) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/consistency-tradeoffs-in-modern-distributed-database-system-design.pdf) |
| `#34` | *Anna: A KVS for Any Scale*（无共享 Actor 线程、基于有界半格 Lattice 结合交换幂等合并兑现无协调因果/读已提交多级一致性） | 18 | [专题 09 · `#paper-34`]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}}#paper-34) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/anna%3A-a-kvs-for-any-scale.pdf) |

### 十、分布式共识协议：从 Paxos 泛化到 Raft 工程边界（5 篇）

| 编号 | 论文题目 / 精读焦点 | 页数 | 精读章节 | 原始文献 |
|---:|---|---:|---|---|
| `#32` | *Distributed Consensus Revised*（Heidi Howard 博士论文：抽离 Paxos 本质不变量，打破 Phase-1 与 Phase-2 必须同为严格多数派的枷锁） | 151 | [专题 10 · `#paper-32`]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}#paper-32) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/distributed-consensus-revised.pdf) |
| `#30` | *A Generalised Solution to Distributed Consensus*（Howard & Mortier 2019：单次写持久寄存器集抽象、1-RTT 快速恢复交集与主备重配置统一框架） | 20 | [专题 10 · `#paper-30`]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}#paper-30) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/a-generalised-solution-to-distributed-consensus.pdf) |
| `#33` | *In Search of an Understandable Consensus Algorithm (Extended Version)*（Raft 扩展版：强 Leader 单向日志流、选举日志新旧限制、幽灵复现 Figure 8 禁令与 Joint Consensus） | 18 | [专题 10 · `#paper-33`]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}#paper-33) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/in-search-of-an-understandable-consensus-algorithm-%28extended-version%29.pdf) |
| `#31` | *Consensus: Bridging Theory and Practice*（Diego Ongaro 博士论文：单节点增删成员变更、日志快照截断、ReadIndex 线性一致读与客户端去重会话表） | 258 | [专题 10 · `#paper-31`]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}#paper-31) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/consensus%3A-bridging-theory-and-practice.pdf) |
| `#35` | *Ark: A Real-World Consensus Implementation*（MongoDB 复制集原型：拉模式日志同步、无显式持久 `votedFor` 下的选举碰撞防线与跨任期提交回滚治理） | 12 | [专题 10 · `#paper-35`]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}}#paper-35) | [PDF](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/ark%3A-a-real-world-consensus-implementation.pdf) |

---

## 文献归档说明

本系列精读所引用的原始论文 PDF 统一锚定开源文献库 [`lonng/db-papers`](https://github.com/lonng/db-papers)（归档提交前缀 `4ff1331`），其中 `#52`（Flajolet & Martin 1985）直接链接至 INRIA 作者公开归档页面。各专题正文中所引页码（如 `p.3`、`p.11`）均以对应归档 PDF 的物理页序为准。

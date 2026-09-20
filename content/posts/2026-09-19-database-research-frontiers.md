---
title: "【论文】2026 数据库内核研究进展：从因子化执行到自适应优化与增量维护"
date: 2026-09-19T00:00:00+08:00
lastmod: 2026-09-19T00:00:00+08:00
slug: "database-research-frontiers-2026"
categories:
  - 数据库
tags:
  - 论文阅读
  - 查询执行
  - 查询优化器
  - 存储引擎
  - 增量计算
  - VLDB
  - SIGMOD
description: "围绕 SIGMOD、VLDB、OSDI、FAST 与 CIDR 的近期论文，分析数据库优化对象如何从单个算子扩展到中间表示、数据子集、后台维护和状态演化，并连接已有的 Umbra、学习优化器与云存储阅读路线。"
draft: false
---

读完一轮数据库新论文后，最容易留下的印象是系统又多了几个标签：GPU、LLM、存算分离、自动布局、增量计算。但这些标签并不能解释，为什么一个已有向量化执行、成本优化器和对象存储的系统，仍然会在某些负载下表现得很差。

把标签拿掉，这批工作反复触及几个更具体的问题：Join 生成的重复组合是否必须立即展开？同一条 SQL 的不同数据是否应该走同一计划？一次局部便宜的刷新是否会让下游更贵？今天为了压低读延迟而推迟的 compaction，明天会以什么形式回来？

**我的主要判断是：数据库的优化对象正在从“如何更快执行这个算子”，扩展到“以什么表示、对哪部分数据、在什么状态下、承担多少后续工作”。** 这不是对整个领域的统计结论，而是下面这些论文共同呈现出的研究方向。

这也延续了此前的 [Umbra 研究路线]({{< relref "2026-09-04-dive-umbra.md" >}}) 和 [数据库系统论文精读]({{< relref "2026-08-23-database-paper-reading-notes.md" >}})：前者讨论执行器怎样逐步放松理想化假设，后者讨论现代硬件、工业系统和自治机制。本文进一步关注这些机制之间的边界，以及下一步值得验证的组合。

## 核心判断

1. **少产生中间数据，正在成为与加速算子同等重要的执行问题。** SplitJoin 改变数据子集的计划，FFX 改变中间结果的表示，两者分别减少不必要的中间工作和重复展开。
2. **优化器的难点不只在预测精度，也在候选集合和反馈成本。** OBELISK 研究如何有限预算地探索计划，APQO 面对计划缓存本身会变化的问题。
3. **存算分离需要重新组织整个数据路径。** 只卸载 compaction，未必解决 memtable、flush、网络和读路径的瓶颈；前台与后台各自最优，也未必组成一个稳定系统。
4. **增量计算正在从代数变换走向维护决策。** 能生成 delta plan，只回答了“能不能”；历史成本、下游变化和布局收益，才回答“什么时候值得”。
5. **这些方向都需要把状态纳入评价。** 计划缓存、历史反馈、维护积压和布局都会变化；一次静态 benchmark 很难充分说明长期收益。

## 一、先明确“最新”与“读过”的含义

本文检索截止于 **2026 年 9 月 19 日**，从 [VLDB 2026](https://vldb.org/2026/program.html)、[SIGMOD 2026](https://2026.sigmod.org/sigmod_papers.shtml)、[OSDI 2026](https://www.usenix.org/conference/osdi26/technical-sessions)、[FAST 2026](https://www.usenix.org/conference/fast26/technical-sessions)、[CIDR 2026](https://www.cidrdb.org/cidr2026/program.html) 和 [SOSP 2025](https://sigops.org/s/conferences/sosp/2025/accepted.html) 的官方目录筛选，再回到原文、作者页面或出版方摘要。

这里包含不同出版体系：VLDB 对应 PVLDB，SIGMOD Research 对应 PACMMOD，OSDI、FAST、SOSP 是会议。APQO 的期刊出版时间是 2025 年 12 月，但属于 SIGMOD 2026；FFX 在 2026 年 9 月上传 arXiv，不代表它到 9 月才正式发表。本文没有穷尽 PVLDB v20、全部 online-first 和 2027 已接收稿，因此“最新”指本轮核实的近期研究窗口。

阅读深度也需要分开：

| 材料 | 本文依据 | 如何使用 |
|---|---|---|
| FFX、SplitJoin、Sirius、OBELISK、O3-LSM、HATS、LogDrive、Enzyme、WAIR | 原文重点章节与实验说明 | 分析机制及适用边界 |
| APQO | 出版方摘要 | 判断问题定位，保留实验核对事项 |
| Mantle | 作者正文架构片段 | 讨论职责划分，不推断完整恢复协议 |
| AutoLiquid | 已有公开设计材料、此次发现的正式论文入口 | 延续预读状态，不冒充全文精读 |
| Semirings | 预印本摘要与定理陈述 | 讨论语义边界，不推广复杂度结论 |

本文未运行这些论文的 artifact。涉及实验的数字均来自作者报告；文中的简化例子和工程实验建议，则是为了说明问题而构造的分析。

## 二、查询执行：优化生成了什么，而不只是处理得多快

### 2.1 FFX：把重复关系留在表示里

[FFX：Factorized and Vectorized Execution](https://amine.io/papers/2026-sigmod-ffx.pdf)（SIGMOD 2026）处理的是一个经常被向量化吞吐掩盖的问题：执行器可能在高效地处理大量本来不必展开的重复值。

用一个简化例子说明。假设固定键 `a` 分别连接三个 `b` 和三个 `c`，且这两组选择之间没有额外约束：

```text
展开的中间结果：             因子化的概念表示：
(a, b1, c1)                  a → B = {b1, b2, b3}
(a, b1, c2)                    → C = {c1, c2, c3}
...                           含义仍是 B × C
(a, b3, c3)
共 9 个组合
```

如果后续操作允许利用共享结构，就没有必要立即复制九份 `a`。这不是去重：SQL 的 multiplicity 仍然存在，只是用更紧凑的物理结构表示它。

FFX 使用 packed factorized vectors，把共享结构与批量计算结合起来，并通过 cascade update 传播层次间的过滤结果。它同时保留退回传统向量化表示的路径。论文还将紧凑表示用于语义查询中的 LLM 输入，研究序列化大小与输出质量之间的关系。

它对已有 Diamond Hardened Join 阅读路线的推进是：从“把 Lookup 与 Expand 拆开、尽量晚扩张”，进一步走向“跨算子传递什么样的中间表示”。

我的工程判断是，表示转换应成为优化器能够计价的决策。高重复、多对多连接可能从中受益；普通主外键查询未必值得承担额外结构成本。若用户最终要求全部九个组合，输出枚举仍然要发生。**因子化能够推迟或避免不必要的展开，不能消除语义要求的输出。**

### 2.2 SplitJoin：同一 Join order 不必适合所有数据

[One Join Order Does Not Fit All](https://arxiv.org/html/2510.25684v1)（VLDB 2026）从另一个位置处理中间结果问题。

传统计划通常为整条查询选择一个 Join order。但平均选择率可能掩盖局部结构：某些连接键具有很高度数，另一些键很稀疏；适合多数键的顺序，可能在少数热点键上产生巨大中间结果。

SplitJoin 根据连接键度数等信息划分 heavy/light 部分，为不同部分选择计划，并复用现有二元 Join 引擎。研究的关键不是“发现倾斜后多开线程”，而是允许不同数据部分采用不同的连接顺序。

作者在论文社交网络查询负载上报告，DuckDB 平均获得约 2.1 倍运行时间改善和 7.9 倍中间结果缩减。其理论算法有查询结构、关系规模和划分规则等前提，实际方案则使用启发式；不能把理论最坏情况保证直接推广到所有 SQL 和实际实现。

从工程角度看，新的自由度也引入新的账单：划分需要统计和处理，可能增加扫描，优化器要搜索更多选择。若输入本来接近均匀，划分开销可能无法回收。

### 2.3 Prune、Split、Factorize、Expand 是四件不同的事

这组论文最适合与已有 Hash Join、Dynamic Filter 和 Umbra 笔记一起读，因为它们改变的对象不同：

| 动作 | 改变什么 | 无法单独解决什么 |
|---|---|---|
| Prune | 提前删除不能参与结果的输入 | 大量有效匹配形成的重复组合 |
| Split | 为数据子集选择不同计划 | 同一子集内仍然存在的表示重复 |
| Factorize | 紧凑表示有效组合 | 任意算子都能直接消费该表示的问题 |
| Expand | 将紧凑结构转换为平坦元组 | 已经在上游浪费的计算与内存 |

据此可以提出一个组合实验：在同一组重复键和倾斜查询上，逐步开启过滤、划分和因子化，观察中间行数、峰值内存、转换成本与总延迟。这个实验建议不意味着这些机制已经能够直接组合，更不意味着组合必然优于单项。

### 2.4 Sirius：GPU 后端的价值要从查询入口量到结果出口

[Sirius：Rethinking Analytical Processing in the GPU Era](https://www.cidrdb.org/cidr2026/papers/p12-yogatama.pdf)（CIDR 2026）探索复用现有前端、以 Substrait 对接 GPU 执行后端的方式，使用 GPU 算子库承接分析处理。

这条路线的重要性在于复用边界：SQL 前端与优化器不必随硬件全部重写，但跨边界的物理语义、数据位置和算子覆盖会成为约束。

论文单机 TPC-H 实验使用 SF100，采用跨云供应商等小时租金比较；分布式实验仅覆盖当时支持的 Q1、Q3、Q6。它提供了明确的系统原型证据，却不足以支持“GPU 已全面替代 CPU”的判断，论文租金也不是当前采购报价。

对工程评估，我更关心以下总路径：

```text
读入与解码 → 主机/设备搬运 → GPU 执行 → 不支持算子的回退 → 结果交付
                              │
                              └─ 显存不足、并发、spill
```

如果只量中间的 kernel，可能忽略最有决定性的成本。这与 FFX 是同一个评价问题的不同侧面：既要问每秒处理多少数据，也要问为什么要搬运和处理这么多数据。

## 三、优化器：从一次预测转向持续管理候选与反馈

### 3.1 OBELISK：离线搜索的核心是如何花预算

[OBELISK](https://www.vldb.org/pvldb/vol19/p1674-pan.pdf)（PVLDB 19(7), 2026）通过成本缩放旋钮影响现有 CBO 的候选计划，结合 Bayesian optimization、LLM 推理和历史评估信息，减少低价值或重复的探索。

在这里，LLM 参与有反馈的搜索过程；既有优化器仍然负责产生计划。Training-free 表示不需要该方案的专门训练，并不意味着没有推理费用、候选运行时间和超时损失。

它与 LOAM、Ultron 等工作的对照点，不应只剩“是否使用大模型”，而应拆成三问：候选计划从哪里来？用什么证据判断候选？探索支出由多少次后续执行摊销？

可以用下面的简化不等式表达我的工程判断，它不是论文公式：

```text
未来执行次数 × 单次节省
    > 候选探索成本 + 模型调用成本 + 后续验证与失效维护成本
```

高频、稳定、昂贵的模板更容易满足这个条件；一次性查询则可能不值得进入深度离线搜索。还需要明确，当 schema、统计或引擎版本变化时，历史赢家是否继续有效。否则离线优化得到的资产，也可能成为长期回归的来源。

### 3.2 APQO：缓存里的计划增加后，模型怎么办

[APQO：An Adaptive Framework for Parametric Query Optimization](https://doi.org/10.1145/3769761)（PACMMOD 2025，SIGMOD 2026）关注参数化查询和变化中的计划集合。

根据出版方摘要，APQO 同时使用参数和计划表示，通过离线预训练与在线校准适应动态计划缓存和分布变化。它所处理的结构性问题是：如果预测器只是把参数映射到固定计划类别，那么新增一个计划，就可能改变原来的预测任务。

这值得接在 Kepler、ScalePQO 后阅读。比较时需要固定问题口径：候选集相同吗？新增计划需要多少样本？参数分布变化后多久恢复？校准的执行费用如何计入？

本轮尚未核对 APQO 全文实验，因此不能根据“foundation model”这个名称推导任意数据库的零样本迁移能力。它在本文中提供的是下一步阅读的问题定位。

### 3.3 把学习优化器拆成四个可独立检验的部分

结合上述工作，我倾向于使用下面的结构分析优化器，而不是笼统评价“模型准不准”：

| 部分 | 要回答的问题 | 失败时的实际表现 |
|---|---|---|
| 候选生成 | 好计划是否进入候选集 | 再准确的排序也选不到好计划 |
| 候选评价 | 预测依据是否匹配当前参数和状态 | 把历史快计划误判为当前快计划 |
| 反馈采集 | 获取样本需要付出多少执行成本 | 搜索节省小于探索支出 |
| 失效与回退 | 哪些变化需要重新验证 | 旧知识在新版本上持续造成回归 |

这个拆分也方便接入数据库测试：选择合法计划与判断哪个计划更快是不同问题；模型置信度不能代替结果正确性验证。

## 四、存储引擎：卸载之后，瓶颈和债务去了哪里

### 4.1 O3-LSM：完整写路径不能只看 compaction

[O3-LSM](https://cs.purdue.edu/homes/csjgwang/pubs/SIGMOD26_O3LSM.pdf)（SIGMOD 2026）把卸载范围扩展到 memtable、flush、compaction 三层，并利用 key-range shard 暴露并行度，同时考虑读委托与缓存。

这解释了一个容易忽略的现象：compaction 从计算节点搬走后，写入吞吐仍可能被 buffer 容量、flush 或网络限制。优化一个阶段后，剩余阶段占据关键路径，系统并不会自动变成可无限扩展的写入服务。

我的阅读重点是把数据路径和可靠性边界分别画出来：

```text
写入接收 → memtable → flush → 文件与 compaction
   │           │         │             │
   └─ ACK      └─ 位置   └─ 资源调度   └─ 读放大与恢复
```

图中是需要核对的职责，不是对论文 ACK 协议的复述。实际采用此类设计前，应明确：确认写入时哪些副本已满足持久性？远端内存失效如何处理？网络饱和时谁进行反压？卸载后读路径付出了什么代价？

因此，不能把特定网络与内存池环境中的最大吞吐改善，直接换算成普通部署的收益。

### 4.2 HATS：前台延迟与后台积压属于同一个问题

[HATS](https://www.usenix.org/system/files/fast26-ren.pdf)（FAST 2026）在 Cassandra 上联合考虑粗粒度读分配、细粒度副本协调和 compaction 速率控制。

它的启发在于两个局部最优会冲突：只平衡前台读，可能把请求送到正受后台任务干扰的节点；只为当前读延迟压低 compaction，又会让积压和读放大继续增加。

这可以用一个自定义的时间序列例子理解：今天暂停维护，P99 降低；随后文件或层级重叠增加，同样一次读取要承担更多工作；积压最终集中偿还，尾延迟重新升高。第一阶段的“优化”可能只是把成本推迟了。

因此我会同时记录四条曲线：前台延迟、后台资源使用、维护积压、读放大。只有前台变快且积压保持可控，才更接近持续收益。

这条路线与分析系统中的后台 Merge 有可借鉴之处，但 Cassandra 的副本路由不能直接移植到文件合并系统。应先建立资源争用和维护积压的对应关系。

### 4.3 LogDrive：持久性与顺序可以分别设计

[The LogDrive: Composable Durability for Cloud-Based Shared Logs](https://www.usenix.org/system/files/osdi26-vickers.pdf)（OSDI 2026）将持久性底座 LogDrive 与提供有序共享日志语义的 AtomicLog 区分开，Conflux 再基于它们实现复制状态，用于 Confluent 的元数据服务。

这种职责分离允许系统组合底层云存储，而不把持久化与排序固定在同一种服务实现中。它并没有取消一致性协议。

对云数据库，元数据的请求费用、batch 方式和恢复路径同样会影响总体成本。把低容量单价等同于低系统成本，往往会漏掉高频小请求和协调工作。阅读论文中的成本优势时，必须保留服务类型、负载和延迟 SLA 的前提。

### 4.4 Mantle：快速路径与权威状态分别放在哪里

[Mantle](https://madsys.cs.tsinghua.edu.cn/publication/mantle-efficient-hierarchical-metadata-management-for-cloud-object-storage-services/SOSP25-Li.pdf)（SOSP 2025）值得接在 HopsFS、Tectonic 后面阅读。

本轮核对的作者正文片段描述了分片 TafDB 与每个 namespace 的轻量 IndexNode 的分工：海量权威元数据与目录快速查询路径不必采用同样的组织方式。层次目录看似只是路径字符串，实际涉及多轮解析、热点和更新争用。

这给下一轮精读留下三个具体问题：rename 如何影响索引和缓存？副本读允许什么一致性边界？故障后怎样重新建立快速路径？这些问题需要全文协议支持，本文不把它们当作已核实的机制。

## 五、增量计算：决定维护什么、何时维护、维护到哪里

### 5.1 Enzyme：单个视图的最优选择可能让下游更贵

[Enzyme: Incremental View Maintenance for Data Engineering](https://arxiv.org/html/2603.27775v2)（SIGMOD 2026 Industry）将增量维护放回真实的数据工程流程：规范化计划、识别可用刷新策略，再结合历史执行反馈估算成本。

特别值得关注的是 MV 依赖图。上游采用全量重算，即使在本节点更便宜，也可能向下游产生更多变化；因此逐视图选择最便宜策略，未必等价于全图最优。

用一个虚构数字例子说明，数字不是论文实验：

| 上游策略 | 上游成本 | 对下游产生的影响 | 下游成本 | 合计 |
|---|---:|---|---:|---:|
| 全量刷新 | 8 | 产生大量待处理变化 | 20 | 28 |
| 增量刷新 | 10 | 保留较小变化集 | 3 | 13 |

表中真正的变量不是“8 对 10”，而是上游选择改变了下游输入状态。实际系统是否产生这种变化流，取决于刷新和变更捕获语义，不能假设所有全量刷新都有相同后果。

论文 TPC-DI 的八个案例中，成本模型选对七个；作者也将完整成本模型的论述留给后续工作。这意味着它提供了重要方向，却不能据此重建整个生产实现，更不能得出增量永远优于全量的结论。

对自己的实验，我会在 delta 行数之外记录：变化命中的历史状态规模、删除比例、join 扇出、扫描与写回、下游变化大小和恢复成本。小 delta 不一定意味着小工作量。

### 5.2 WAIR：维护结果之外，还可以增量维护布局

[Workload-Aware Incremental Reclustering in Cloud Data Warehouses](https://arxiv.org/html/2602.23289v2)（SIGMOD 2026）讨论的是布局维护策略，而不是 SQL 结果的 IVM。

WAIR 将 clustering key 的选择与 reclustering policy 区分开，关注查询范围边界上的 micro-partitions：它们与谓词部分相交，可能导致额外扫描；完全位于查询范围内部或外部的分区，对这个谓词未必值得重新整理。

例如，为解释边界扫描而假设查询范围为 `[40, 60]`：

```text
分区范围       与查询的关系          对这个查询的直接含义
[0, 30]        完全在外部            可被范围统计排除
[25, 55]       部分相交              扫描中含有不需要的数据
[50, 80]       部分相交              扫描中含有不需要的数据
[85, 100]      完全在外部            可被范围统计排除
```

这是简化示意，不是 WAIR 完整算法。它说明布局“是否整齐”不是最终目标，未来扫描能省多少才是。历史 workload 能否代表未来，也因此成为关键假设；论文理论成本界不能脱离其模型条件使用。

### 5.3 AutoLiquid：选什么 key，与重写哪些文件是两个层次

[AutoLiquid: Autonomic Data Layout Optimization for the Databricks Lakehouse](https://www.vldb.org/pvldb/vol19/p4023-liang.pdf) 已在 VLDB 2026 Industry 目录中提供正式论文入口。此前阅读笔记把它标为公开设计预读，本轮全文提取仍不稳定，因此维持这个证据等级。

公开材料中的 workload 观测、候选 key、抽样验证和应用过程，可以用来提出它与 WAIR 的分工问题：前者帮助讨论“选择什么布局方向”，后者帮助讨论“如何逐步支付重写成本”。两者是否能直接组合，仍需核对各自目标函数与假设。

目前不应补写未经核实的内部阈值、曲线结构或完整控制策略。下一步阅读产物应是一张决策依赖图，而不只是把两个系统归入“自动聚簇”。

### 5.4 Semirings：复杂度结论首先依赖语义

[The Role of Semirings in Incremental View Maintenance](https://arxiv.org/abs/2606.07795v2) 是本轮补充的预印本，v2 更新于 2026 年 9 月 12 日，尚未核实正式会议归属。

论文研究 semiring-annotated 数据库的 insert-only 维护，对特定查询类和半环条件给出复杂度分类。它提醒我们：讨论更新代价之前，要先明确维护的是集合存在性、重复次数，还是带来源的注释。

直观上，同一个结果元组出现两次，在集合语义中仍只是“存在”，在 bag 语义中却有不同 multiplicity；如果需要保留来源，维护的对象又会变化。这是理解问题的例子，不是对论文定理的替代证明。

本轮只核对摘要与定理陈述，因此不将受查询结构、半环性质等条件限制的结果推广到删除、任意 SQL 或外连接。它适合作为 DBSP 阅读后的理论补充，帮助列清楚语义前提。

## 六、这些论文共同推动了怎样的成本模型

把各方向放在一起，可以看到成本模型的输入正在扩展。下面是我的综合分析框架，并非某篇论文给出的统一模型：

```text
一次执行的代价
  = 访问与传输
  + 算子计算
  + 中间表示及转换
  + 结果交付

一段时间内的系统代价
  = 查询执行
  + 候选探索与反馈采集
  + 状态和布局维护
  + 变化向下游传播
  + 失败恢复与重新验证
```

这些项不能简单独立相加后就认为问题解决了：因子化会改变中间规模，布局会改变未来扫描，后台维护会改变前台竞争，上游刷新会改变下游输入。研究价值恰恰在于识别并控制这些相互作用。

| 论文方向 | 新纳入决策的对象 | 应重点观察的代价 |
|---|---|---|
| FFX / SplitJoin | 中间表示、数据子集 | 展开、划分、峰值内存 |
| Sirius | 执行硬件与数据位置 | 搬运、回退、显存竞争 |
| OBELISK / APQO | 候选空间、历史状态 | 探索、校准、失效 |
| O3-LSM / HATS | 卸载位置、后台任务 | 网络、干扰、维护积压 |
| LogDrive / Mantle | 持久性、排序、目录快速路径 | 请求费用、协调、恢复 |
| Enzyme / WAIR | 刷新策略、布局重写 | 下游变化、重写与未来收益 |

因此，评价这些系统时，平均加速比只是一个切面。还应问：在哪些负载上退化？回退有什么固定成本？反馈过期多久？如果把观察窗口拉长，后台工作是否保持稳定？

## 七、下一步阅读与实验路线

结合已有笔记，我会把后续工作安排为三个阶段。

**第一阶段：先打通中间结果与维护代价。** 精读顺序为 SplitJoin → FFX → Enzyme → WAIR。前两篇回答执行时如何减少中间工作，后两篇回答重复运行时如何减少维护工作。配套实验分别采用有倾斜的多表 Join 与两级 MV DAG，并保留均匀数据、低重复数据和全量重算作为对照。

**第二阶段：把反馈和资源竞争补进系统模型。** 阅读 OBELISK → APQO，以及 O3-LSM → HATS。优化器实验计入候选执行和校准预算；存储实验同时观察前台 P99、后台积压和恢复到稳态的时间，避免只比较短时间吞吐。

**第三阶段：补齐云端状态与语义边界。** 阅读 LogDrive → Mantle，并将 Sirius 与 Semirings 分别作为硬件路径和理论条件的专题。AutoLiquid 的正式全文核对也在此阶段补齐，再与 WAIR 比较完整决策流程。

最终希望得到的不是一份越来越长的论文目录，而是几组可复用的判断：什么时候该减少数据，什么时候该改变表示，什么时候应该花钱探索，以及什么时候暂缓维护只是在积累未来成本。它们比单篇论文的最高加速比，更能指导下一次内核设计和性能诊断。

## 参考文献

以下保留正式出版入口或本轮核对的作者原文；会议年份与预印本状态按前文口径区分。

1. [Factorized and Vectorized Execution: Optimizing Analytical and Semantic Queries over Relations](https://doi.org/10.1145/3802055). SIGMOD 2026.
2. [One Join Order Does Not Fit All: Reducing Intermediate Results with Per-Split Query Plans](https://arxiv.org/abs/2510.25684). VLDB 2026.
3. [Rethinking Analytical Processing in the GPU Era](https://www.cidrdb.org/cidr2026/papers/p12-yogatama.pdf). CIDR 2026.
4. [OBELISK: Efficient Offline Query Planning with Bayesian Optimization-Informed Language Model Reasoning](https://www.vldb.org/pvldb/vol19/p1674-pan.pdf). PVLDB 19(7), 2026.
5. [APQO: An Adaptive Framework for Parametric Query Optimization](https://doi.org/10.1145/3769761). PACMMOD 3(6), 2025 / SIGMOD 2026.
6. [O3-LSM: Maximizing Disaggregated LSM Write Performance via Three-Layer Offloading](https://doi.org/10.1145/3802093). SIGMOD 2026.
7. [Holistic and Automated Task Scheduling for Distributed LSM-tree-based Storage](https://www.usenix.org/conference/fast26/presentation/ren). FAST 2026.
8. [The LogDrive: Composable Durability for Cloud-Based Shared Logs](https://www.usenix.org/conference/osdi26/presentation/vickers). OSDI 2026.
9. [Mantle: Efficient Hierarchical Metadata Management for Cloud Object Storage Services](https://doi.org/10.1145/3731569.3764824). SOSP 2025.
10. [Enzyme: Incremental View Maintenance for Data Engineering](https://arxiv.org/abs/2603.27775). SIGMOD 2026 Industry.
11. [Workload-Aware Incremental Reclustering in Cloud Data Warehouses](https://doi.org/10.1145/3802127). SIGMOD 2026.
12. [AutoLiquid: Autonomic Data Layout Optimization for the Databricks Lakehouse](https://www.vldb.org/pvldb/vol19/p4023-liang.pdf). VLDB 2026 Industry；本文保持设计预读状态。
13. [The Role of Semirings in Incremental View Maintenance](https://arxiv.org/abs/2606.07795v2). arXiv 预印本，v2 更新于 2026-09-12。

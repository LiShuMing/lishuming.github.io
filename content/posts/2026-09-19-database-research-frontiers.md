---
title: "【论文】2026 数据库内核研究进展：从因子化执行到自适应优化与增量维护"
date: 2026-09-19T00:00:00+08:00
lastmod: 2026-10-18T00:00:00+08:00
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
description: "当向量化与 JIT 编译已把单算子吞吐推近硬件极限，数据库内核还能去哪找数量级收益？从 SIGMOD/VLDB/OSDI/FAST 2026 的代表工作出发，拆解中间表示、分片计划、反馈预算、存储债务与增量维护这笔系统总账。"
draft: false
toc: true
math: true
---

> 本系列第 1/3 篇 · 论文雷达 · [下一篇]({{< relref "2026-08-09-sigmod-2026-industry-papers.md" >}}) · 对照阅读：[Umbra 研究路线]({{< relref "2026-09-04-dive-umbra.md" >}}) · [现代硬件]({{< relref "2026-11-19-hardware-evolution-and-database-programming-paradigms.md" >}}) · [经典论文精读]({{< relref "2026-08-23-database-paper-reading-notes.md" >}})

过去十几年里，分析型数据库内核最核心的军备竞赛是**让单个算子每秒吞掉更多元组**——列式内存布局、SIMD 向量化批处理、基于 LLVM 的查询编译（JIT），把单核 CPU 的指令流水线越压越满。

但当算子本身的循环已经快到逼近内存带宽时，再抠几条指令很难换来数量级的提升。这时候回头拆解一次复杂查询和一套在线数仓的真实开销，就会发现大头往往浪费在四个地方：

1. **算得飞快，但算了太多本不必展开的中间行**（多对多连接里的笛卡尔膨胀、少数倾斜热点键拖垮全局连接顺序）；
2. **计划选错，或者花重金搜出来的“好计划”还没跑回本就过期了**；
3. **把存储引擎的某一段卸载到远端或压低后台任务，结果瓶颈转移到上游缓冲，或者攒成了明天的尾延迟债务**；
4. **只盯着单次查询或单个物化视图省钱，却让下游视图刷新和日常文件重排付出了双倍代价**。

翻开 2026 年 SIGMOD、VLDB、OSDI、FAST 和 CIDR 上的数据库内核论文，会发现大家不约而同地从“单算子微优化”转向了这四笔**跨阶段总账**。下面我们就顺着“查询执行 → 优化器反馈 → 存储分层与调度 → 增量计算与数据布局”这条脉络，把这些新机制放回第一性原理里看个究竟。

---

## 一、查询执行：与其搬运得更快，不如一开始就少生成中间数据

一条多表连接慢，最直观的反应往往是“Hash 表探测不够快”或者“上 GPU 加速”。但在很多真实负载（尤其是图分析、社交关系或带多值标签的分析查询）里，CPU 忙活半天，其实是在反复复制一模一样的数据。

### 1.1 FFX：把多对多连接的“乘法膨胀”留在紧凑表示里

设想一个最简单的三表关联：同一个用户 `a` 命中了 3 条浏览记录 `{b1, b2, b3}`，同时又命中了 3 张优惠券 `{c1, c2, c3}`，而且这两组条件彼此独立。在传统的平坦元组（Flat Tuple）模型里，连接算子必须老老实实把结果物化成 3 × 3 = 9 行：

```text
传统向量化展开（立刻生成 9 行）：     因子化紧凑表示（保留乘法结构）：
(a, b1, c1)                         a ──→ B = {b1, b2, b3}
(a, b1, c2)                           └─→ C = {c1, c2, c3}
...                                 （语义上仍代表 B × C 共 9 个组合）
(a, b3, c3)
```

如果下游算子只是按 `a` 做聚合、过滤，或者把关联上下文喂给大模型做语义判断，先把 `a` 复制九遍纯属无用功。注意，不展开**绝不等于 `DISTINCT` 去重**——SQL 要求的多重集基数（Multiplicity，即每个值出现了几次）一分都不能少，只是我们换了一种不占内存的物理编码来表达它。

SIGMOD 2026 的 **[FFX（Factorized and Vectorized Execution）](https://amine.io/papers/2026-sigmod-ffx.pdf)** 解决的正是“因子化执行（Factorized Execution）怎样跟现代列式向量化引擎合体”的难题。它设计了紧凑的打包因子化向量（Packed Factorized Vectors），让批量算子直接在未展开的共享块上做计算，并用级联更新（Cascade Update）把上层过滤条件同步到各个分支，遇到无法直接消费因子化结构的算子时再按需退回常规向量（同期 VLDB 2026 上 Lehner 与 Neumann 的 [《The Data World Is Not Flat》](https://www.vldb.org/pvldb/vol19/p3006-lehner.pdf) 则从编译型引擎视角探索了另一条因子化落地路径）。论文还顺手验证了一个很有意思的副产品：在关系数据上跑 LLM 语义查询时，把因子化后的紧凑结构序列化喂给模型，能显著压缩 Prompt 长度。

从内核工程视角看，因子化本质上是一种**用表示维护开销换取免除笛卡尔拷贝**的交易：在多对多连接、高重复度星型扩展上收益惊人；但如果走的是常规的一对一主键外键关联，或者最终 `SELECT *` 还是要立刻把所有组合原样吐给客户端，额外维护这套树状向量的固定成本就未必划得来。

### 1.2 SplitJoin：既然热点键和长尾键脾气相反，为什么强迫它们走同一个连接顺序？

除了把重复值压紧，减少中间结果的另一条路是反思一个延续了几十年的默认前提：**整条 SQL 查询只能选唯一一个连接顺序（Join Order）吗？**

传统优化器算选择率看的是全表统计均值。但在真实数据里，连接键的度数往往高度倾斜——99% 的普通用户只有几条邻居边，先连哪张表都很轻巧；唯独 1% 的超级大 V 拥有几十万条边，一旦按“全局平均最优”的顺序先连，瞬间就会炸出几亿行中间结果。以前引擎遇到倾斜只会在执行期打补丁（比如把大 V 切碎分发给更多线程），但“先连 A 还是先连 B”的计划本身已经锁死了。

VLDB 2026 的 **[SplitJoin（One Join Order Does Not Fit All）](https://arxiv.org/html/2510.25684v1)** 直接打破了这个捆绑：它根据连接键的度数把输入切分成重度（Heavy）和轻度（Light）等不同分片，**允许不同数据子集各自走最契合自己的二元连接顺序**，底层依然复用成熟的常规 Hash Join 算子，无需推翻重写执行引擎。在论文测试的社交网络倾斜负载上，合入 DuckDB 的原型把平均执行时间压低了约 2.1 倍，中间结果行数更是缩减了 7.9 倍。

把 ZoneMap 与 Runtime Filter（动态过滤）、SplitJoin、FFX 以及出口处的延迟物化串成一条端到端流水线，就能看清现代执行引擎如何在 `Prune → Split → Factorize → Expand` 四个环节逐级削减无用功：

![现代查询执行引擎四阶减负流水线：Prune → Split → Factorize → Expand](/images/diagrams/dbres-prune-split-factorize.svg)

对照四把手术刀各自的切入位置与回本边界，工程取舍会更加清晰：

| 机制动作 | 动手脚的对象 | 省下了什么开销 | 什么时候赚不回票价 |
| --- | --- | --- | --- |
| **Prune（动态裁剪）** | 输入数据源 | 提前丢掉注定连不上的死数据 | 数据全都连得上、且存在多对多膨胀时无能为力 |
| **Split（分片异构计划）** | 连接顺序本身 | 不再让少数热点键绑架全局连接次序 | 数据分布均匀时，统计度数和切分调度的额外扫描纯属倒贴 |
| **Factorize（因子化表示）** | 算子间物理编码 | 推迟甚至免除有效匹配的笛卡尔复制 | 主外键稀疏连接或下游立刻要求平坦展开时，结构转换得不偿失 |
| **Expand（延迟物化/展开）** | 最终输出时机 | 让宽列搬运只发生在真正存活的行上 | 若上游已把中间行放大了十倍，晚展开也救不回前面烧掉的内存 |

### 1.3 Sirius：上 GPU 加速，不能只量中间那几毫秒 Kernel

当数据压缩和计划裁剪做完，硬件加速该怎么接？CIDR 2026 的 **[Sirius](https://www.cidrdb.org/cidr2026/papers/p12-yogatama.pdf)** 给出了一个很务实的架构切法：不从零重写 SQL 解析器和优化器，而是直接复用 DuckDB 等现有前端，通过开源计划中间表示 **Substrait** 把物理计划喂给底层的 GPU 算子库执行。在论文基于 TPC-H SF100 的单机测试（按跨云同等小时租金折算性价比）以及分布式 Q1/Q3/Q6 原型验证中，展示了相当亮眼的吞吐潜力。

不过评估这类 GPU 分析后端时，最忌讳只盯着 GPU Kernel 那几毫秒的算力峰值。真实查询从 DuckDB 逻辑计划经 Substrait 交接给 Sirius 后，还要闯过显存缓冲池（GPU Buffer Manager）驻留检查、PCIe 冷块传输，以及遇到显存溢出或未支持算子/类型时自动退回主机 CPU 执行的完整路径：

![Sirius：DuckDB 内嵌 GPU 原生分析执行与自动回退链路](/images/diagrams/dbres-sirius-gpu-execution.svg)

只要某条复杂 SQL 里夹了一个 GPU 算子库还不支持的字符串函数或窗口逻辑，引发整批中间数据在主机内存与显存之间来回横跳，PCIe 搬运耗时瞬间就会吃掉 GPU 算力省下的全部红利。这和 FFX 讲的其实是同一个道理：**先减少跨边界搬运的字节数，再谈每秒能刷多少字节。**

---

## 二、查询优化器：把“找更好的计划”当成一笔需要回本的投资

哪怕执行引擎准备了再多妙招，只要优化器的基数估计（Cardinality Estimation）偏了几个数量级，照样会挑出灾难性的慢计划。这两年学术界密集引入贝叶斯优化、深度学习和大模型来修补优化器，但如果拨开模型名词看本质，核心矛盾其实就一个：**探测新计划是要花真金白银的，这笔探索费由谁来买单？**

### 2.1 OBELISK 与 APQO：离线搜索与动态计划池的回本账

PVLDB 2026 的 **[OBELISK](https://www.vldb.org/pvldb/vol19/p1674-pan.pdf)** 选了一条很聪明的工程路线：它不让大模型凭空捏造物理计划（那样极易产出语法非法或语义不等价的野生计划），而是保留数据库原有的代价优化器（CBO），通过调整一组“成本缩放旋钮”去诱导 CBO 吐出不同的合法候选计划，再结合贝叶斯优化、LLM 推理和历史运行反馈，尽量用更少的实跑次数锁定好计划（同期 SIGMOD 2026 的 [GenJoin](https://arxiv.org/abs/2411.04525) 则探索了用条件生成模型给出子计划 Hint 来收缩搜索空间）。

读这类离线/反馈式规划工作时，“无需专门预训练（Training-free）”很容易让人误以为它是免费的。实际上，每一次拉起数据库试跑候选计划、每一次调用大模型推理、包括踩中烂计划跑超时的等待，全都是实打实的账单。一个反馈优化方案在生产环境能不能立住，取决于一条朴素的回本不等式：

$$\text{未来重复执行次数} \times \text{单次省下的时间} > \text{候选试跑开销} + \text{模型推理开销} + \text{数据漂移后的重验成本}$$

这也解释了为什么这类方案天然适合报表定时任务、高频参数化查询模板，而对跑一次就扔的即席查询（Ad-hoc Query）毫无性价比。

一旦进入高频参数化查询场景，还会撞上第二个棘手问题：**候选计划池本身是活的。** 早期参数化查询优化（PQO）常把问题建模成固定分类任务——输入绑定参数，从已知的 `K` 个计划里挑一个。可在线上运行久了，DBA 或反馈回路往缓存里塞进了第 `K + 1` 个新计划，固定分类头就傻眼了。SIGMOD 2026 的 **[APQO](https://doi.org/10.1145/3769761)**（本文基于出版方摘要定位其问题域）正是针对这个痛点，把“查询参数特征”和“计划本身的结构表示”联合编码，配合离线预训练与轻量在线校准来适应不断增减的计划缓存。

以后评审任何“AI 赋能优化器”的方案，不妨把它拆成四个独立环节逐一拷问：**好计划进得了候选集吗？打分器扛得住参数倾斜吗？攒够反馈样本要烧多少机器？底层表结构或数据分布变了之后，昨天的“冠军计划”怎么体面退场？** 四个环节只要断一环，预测准确率再高也落不了地。

---

## 三、存储与日志引擎：卸载与限流，往往只是把债挪了地方

存算分离和云原生存储已经是现代数据库的标配。但硬件解耦之后，一个常见的工程幻觉是：“我把最吃 CPU 的那一步搬走了，系统吞吐就该原地起飞。”今年几篇系统顶会论文恰好给这种乐观泼了冷水。

### 3.1 O3-LSM 与 HATS：写路径的木桶效应与后台压实的“高利贷”

在基于 LSM-Tree 的存算分离引擎里，最常规的做法是把重度消耗 CPU 和 I/O 的后台压实（Compaction）卸载到远端节点。但 SIGMOD 2026 的 **[O3-LSM](https://cs.purdue.edu/homes/csjgwang/pubs/SIGMOD26_O3LSM.pdf)** 指出，当 Compaction 不再卡脖子后，计算节点本地的 **MemTable 写入、刷盘（Flush）以及跨网协调**立刻顶上来成了新的木桶短板。因此 O3-LSM 干脆把 MemTable、Flush 和 Compaction 三层全部纳入卸载视野，按 Key 范围切分分片释放并行度，并配套设计了读委托与缓存机制。不过在借鉴这类架构时，一定要先算清持久性确认（ACK）边界：客户端收到“写入成功”那一刻，数据究竟落在了远端易失内存还是持久存储上？网络抖动时反压（Backpressure）怎么传回前台？

如果说 O3-LSM 处理的是空间上的瓶颈转移，那么 FAST 2026 的 **[HATS](https://www.usenix.org/system/files/fast26-ren.pdf)** 处理的就是**时间上的债务转移**。

运维过 Cassandra 或 RocksDB 集群的人都见过一种典型症状：白天业务高峰期怕 Compaction 抢磁盘带宽影响读取 P99，于是手动把后台 Compaction 限速；结果刚爽了两个小时，SSTable 层级和重叠文件越攒越多，一次点查被迫扫十几个文件，**读放大（Read Amplification）**反过来把傍晚的尾延迟彻底打爆。今天省下的后台 I/O，全变成了明天连本带利偿还的高利贷。

HATS 的聪明之处在于把“前台读请求路由到哪个副本”和“后台每个节点以多大速率做 Compaction”拧成一个联合调度闭环：正忙着还压实债的节点少接点读流量，闲下来的副本趁机加速消纳积压。这也给我们做存储评测立了一条硬规矩：**绝不能只截前 10 分钟的 P99 延迟报喜，必须把“前台延迟、后台资源占用、未合并文件积压量、读放大系数”四条时间曲线画在同一张图上**——只有积压不发散的低延迟，才是真本事。

### 3.2 LogDrive 与 Mantle：拆开捆绑职责，别为不需要的语义付云账单

在云基础设施上，省钱的秘诀往往是把以前绑死在一起的两件事拆开：

- **把“字节不丢”和“全局定序”拆开**：OSDI 2026 的 **[LogDrive](https://www.usenix.org/system/files/osdi26-vickers.pdf)**（落用于 Confluent 元数据底座）没有用一个笨重的单体分布式日志包办一切，而是底层用 `LogDrive` 专心组合廉价云存储提供高吞吐持久性（Durability），上层再架一层 `AtomicLog` 专门负责全局严格定序，最后拼出复制状态机 `Conflux`。看这类云上日志论文时要注意，对象存储每 GB 单价虽便宜，高频小批次请求的 API 调用费（PUT/GET 计费）却可能贵得吓人，批量攒写（Batching）策略才是决定总成本的生命线。
- **把“权威元数据存储”和“目录树快路径”拆开**：对象存储上模拟分层文件系统（如 `rename` 一个包含十万文件的目录），最怕每次路径解析都去分布式 KV 里跑好几轮事务。SOSP 2025 的 **[Mantle](https://madsys.cs.tsinghua.edu.cn/publication/mantle-efficient-hierarchical-metadata-management-for-cloud-object-storage-services/SOSP25-Li.pdf)** 将分片化的权威存储 `TafDB` 与每个命名空间轻量的内存目录索引 `IndexNode` 分离，让高频目录遍历走紧凑快路径（至于故障切换与跨目录 `rename` 的精确一致性恢复细节，还有待结合完整协议章节进一步深挖）。

---

## 四、增量维护与数据布局：局部贪心为什么会害惨全局？

现代湖仓一体架构里，表不是写完就静止的冷归档，上面往往挂着一串层层派生的物化视图（Materialized View，MV），底下则是不断追加和重排的微分区（Micro-partitions）。一旦进入持续演进的场景，“只看眼前这一步最省钱”的贪心策略就会频频翻车。

### 4.1 Enzyme 与 Semirings：上游图省事全量重算，下游可能跟着陪葬

假设你有一条两级物化视图链路：`基表 → 视图 V1 → 视图 V2`。今天基表来了大批更新，优化器一算账：对 `V1` 来说，做增量维护（IVM）要查历史状态，成本是 **10**；干脆推倒全量重算只要 **8**！如果按单节点贪心决策，`V1` 毫不犹豫选了全量重算。

结果灾难发生在下游：因为 `V1` 整表重写、丢失了细粒度的变更集（Delta），下游依赖它的 `V2` 被迫跟着咽下整张表规模的变更输入，刷新成本瞬间飙到 **20**，全链路总账变成 8 + 20 = 28；反观上游多花一点力气走增量刷新（成本 10），吐给下游的只是极小的真实净变更，`V2` 花 **3** 就能搞定，总账只有 10 + 3 = 13！

SIGMOD 2026 工业界论文 **[Enzyme](https://arxiv.org/html/2603.27775v2)** 把这个真实痛点摆到了台面上：在数据工程的物化视图有向无环图（DAG）里，**上游的刷新方式直接决定了下游的输入形态**，成本估算必须沿着整张依赖图全局寻优，并利用历史执行反馈持续校准（论文在 TPC-DI 的 8 个典型场景中选对了 7 个）。这也提醒我们，评估增量维护千万别只看“本次变更只有 1000 行”——如果这 1000 行恰好包含大量删除、或者撞上了大表高扇出 Join 的热点键，增量维护翻找历史状态的开销完全可能反超全量扫描。

再往理论深处追一步，近期预印本 **[《The Role of Semirings in Incremental View Maintenance》](https://arxiv.org/abs/2606.07795v2)** 从代数角度点破了一个常被忽略的前提：同样叫“增量维护”，你到底是在维护**集合存在性**（有还是没有）、**多重集计数**（出现了几次），还是**完整的数据血缘注释（Provenance）**？它们对应不同的半环（Semiring）代数结构——在集合语义下幂等的操作，换到计数语义下就必须精确追踪重数，算法复杂度边界也会随之剧变。

### 4.2 WAIR 与 AutoLiquid：别为了“强迫症式的整齐”去重写根本没人查的文件

除了物化视图，湖仓后台最烧钱的另一项家务活是**数据重聚簇（Reclustering / Compaction）**。随着数据流式写入，不同微分区（如 Parquet 文件）的最小/最大值区间会严重重叠，导致范围过滤失效。但如果为了“让全表按时间列排得整整齐齐”而把所有重叠文件通通重写一遍，计算账单会非常肉疼。

SIGMOD 2026 的 **[WAIR（Workload-Aware Incremental Reclustering）](https://arxiv.org/html/2602.23289v2)** 算了一笔极其精明的账。假设线上高频查询的过滤区间集中在 `[40, 60]`，我们看看表里四个微分区各自的处境：

| 微分区值域范围 | 与高频查询 `[40, 60]` 的关系 | 值不值得花钱重排？ |
| --- | --- | --- |
| `[0, 30]` | 完全在外部（Min/Max 直接跳过） | 不值得（反正本来就不读） |
| `[25, 55]` | 跨左边界相交（含大量陪读废数据） | **最值得优先拆开重排** |
| `[50, 80]` | 跨右边界相交（含大量陪读废数据） | **最值得优先拆开重排** |
| `[42, 58]` | 完全落在查询内部（读出来全是干货） | 不值得（重排了也省不下扫描量） |

看明白了吗？**文件排得整不整齐本身不产生一分钱价值，未来查询因此少扫了多少字节才是真金白银。** 完全落在热区外面的文件和完全落在热区里面的文件，不管内部多乱，对 `[40, 60]` 这类查询几乎没有无效陪读；真正害系统多读冤枉数据的，恰恰是卡在查询边界上“半截有用、半截陪葬”的跨界文件！

把上层的 `Enzyme` 与底层的 `WAIR` 合在一张数据流图里看，两者其实都在用全局收益抑制局部盲动——前者沿着物化视图 DAG 按需选择增量合并或受控重算以阻断中间状态放大，后者只针对跨查询热区边界的重叠微分区做局部重排：

![增量维护与数据布局全局账单：Enzyme 视图图寻优与 WAIR 按需局部重排](/images/diagrams/dbres-mv-refresh-and-wair.svg)

如果把 Databricks 在 VLDB 2026 工业界发表的 **[AutoLiquid](https://www.vldb.org/pvldb/vol19/p4023-liang.pdf)** 放在旁边对照，两者的层次感就非常清晰了：AutoLiquid 侧重于观测历史负载、抽样决定**“表应该按哪些列（Clustering Keys）来组织方向”**；而 WAIR 则在选定方向后精打细算**“每次只挑哪几个跨边界文件动手术、分多少步把重写成本赚回来”**。当然，这一切都有一个共同软肋：它们都赌明天的查询分布和昨天差不多——一旦业务风向突变，按旧热点重排的布局就可能竹篮打水。

---

## 五、一张总账单：读新论文时该盯住的四个死角

把上面十几篇工作合拢，其实都在刷新我们对数据库“成本（Cost）”的定义。以前我们习惯优化单次查询的算子耗时，而现代内核必须在**单次执行全链路**和**跨时间周期的系统债**两个维度上同时算账：

```text
单次查询的真实账单 = 读取解码与跨设备搬运 + 算子计算 + 中间表示维护/展开 + 未覆盖回退开销
长周期运行的系统账单 = 前台查询总耗时 + 候选计划探索与校准费 + 后台压实/重排开销 + 下游视图级联放大
```

以后无论是在组会读顶会新论文，还是在生产内核里评审一项新优化，只要顺着下面这张对照表连问四句——**“它在什么数据分布下会倒贴钱？退回常规路径的保底开销多大？攒出来的历史经验多久失效？把时间拉长到一周后后台积压稳得住吗？”**——就能迅速穿透漂亮的加速比曲线，摸到真实的工程边界：

| 演进方向 | 代表工作 | 新增的调度自由度 | 最核心的隐性账单 | 什么场景下反而亏钱 |
| --- | --- | --- | --- | --- |
| **紧凑表示与分片计划** | FFX、SplitJoin | 压缩笛卡尔中间块；按倾斜度分片换连接顺序 | 树状向量维护、度数统计与分片调度 | 主外键均匀关联、无多对多膨胀，或下游立刻全量展开 |
| **异构硬件加速** | Sirius | 借 Substrait 复用 SQL 前端，对接 GPU 算子库 | PCIe 搬运、显存溢出回退、CPU/GPU 横跳 | 算子覆盖率不足引发频繁回退，或高并发显存争用 |
| **反馈与参数化优化** | OBELISK、APQO | 用旋钮诱导 CBO 探索候选；动态适配新增计划池 | 候选实跑试错费、在线校准与旧计划淘汰 | 只跑一次的即席查询，或表结构与数据分布剧烈漂移 |
| **存算分离与联合限流** | O3-LSM、HATS | 三层写路径全卸载；前台路由与后台压实联动 | 跨网持久性确认延迟、压实积压引发的读放大 | 网络带宽受限环境，或只顾压低眼前延迟导致积压雪崩 |
| **云上日志与目录解耦** | LogDrive、Mantle | 持久层与定序层分离；权威存储与内存目录树分离 | 云存储高频 API 请求费、故障恢复与重命名协议 | 小请求未做攒批导致 API 账单失控，或元数据强一致写密集 |
| **全局视图与按需重排** | Enzyme、WAIR、AutoLiquid | 沿依赖图全局选刷新策略；只重排跨查询边界文件 | 历史状态查找、文件重写 I/O 与负载预测偏差 | 单层简单视图，或查询过滤范围随机游走、毫无历史规律 |

如果你想把这些思路转化成手头可验证的小实验，最顺畅的推进顺序是：先在本地 DuckDB 上用倾斜数据集对比 **SplitJoin 与 FFX** 对中间行数和峰值内存的压制效果，再搭一个两层物化视图 DAG 验证 **Enzyme** 中“上游全量 vs 增量”的级联账单，最后再去碰需要长周期观测的 **OBELISK** 探索回本线与 **HATS** 压实尾延迟曲线。

---

## 附录：近期开源引擎与湖仓正确性动态速记

除上述顶会论文外，截至 **2026-10-18** 跟踪开源社区主干时，还有三处与“语义和状态边界”直接相关的工业界进展值得顺手记一笔：

1. **A.1 Apache Iceberg 单调快照时间戳（已合入主干）**：规范与实现现已要求同一分支上新 Snapshot 的 `timestamp-ms` 严格大于父快照。这样时间旅行（Time Travel）和增量消费终于有了单调递增的逻辑序保证，不再怕机器时钟轻微回拨导致增量读取漏数据。
2. **A.2 Iceberg V4 自适应元数据树（AMT）基线合仓**：AMT 相关基线 PR 已陆续合入主干（公开讨论口径 18+1），目前属于底层实现与测试基线推进，尚未正式发布启用 V4 格式。
3. **A.3 DuckDB 外部 Hash Join 落盘后的过滤丢行修复（#25702 → #25718 / #26959）**：此前外部 Hash Join 在内存不足触发 Spill 分轮处理时，若构建出的 Bloom Filter 只覆盖了第一轮 Build 分区的值域，就会把后续分区本该匹配上的行静默过滤掉。这再次印证了我们在 [Dynamic Filter 穿越 Outer Join]({{< relref "2026-08-24-dynamic-filter-outer-join.md" >}}) 中强调的铁律：**运行时过滤器的值域必须完整覆盖全部 Build 数据，任何假阴性（False Negative）都会直接酿成静默错数。**
4. **持续跟踪备忘**：Spark External Lookup Join 提案、Trino Iceberg `MERGE` 隔离性边界、ClickHouse Iceberg 读路径最新提交可见性等议题仍在演进中，待社区定稿后再单独拆篇细聊。

---

## 参考文献与检索说明

> **文献范围说明**：本文主线检索覆盖 [SIGMOD 2026](https://2026.sigmod.org/sigmod_papers.shtml)、[VLDB 2026](https://vldb.org/2026/program.html)、[OSDI 2026](https://www.usenix.org/conference/osdi26/technical-sessions)、[FAST 2026](https://www.usenix.org/conference/fast26/technical-sessions)、[CIDR 2026](https://www.cidrdb.org/cidr2026/program.html) 与 [SOSP 2025](https://sigops.org/s/conferences/sosp/2025/accepted.html) 官方目录（初稿检索截至 2026-09-19，2026-10-18 完成一轮增补核查）。其中 FFX、SplitJoin、Sirius、OBELISK、O3-LSM、HATS、LogDrive、Enzyme、WAIR 对照了论文正文与实验设计；APQO、Mantle、AutoLiquid、Semirings 分别依据出版方摘要、架构章节或预印本定理陈述整理问题边界，文中未运行各论文复现实验包（Artifact）。

1. **FFX** — *Factorized and Vectorized Execution: Optimizing Analytical and Semantic Queries over Relations*. [SIGMOD 2026](https://doi.org/10.1145/3802055) ([PDF](https://amine.io/papers/2026-sigmod-ffx.pdf)).
2. **SplitJoin** — *One Join Order Does Not Fit All: Reducing Intermediate Results with Per-Split Query Plans*. [VLDB 2026](https://arxiv.org/abs/2510.25684).
3. **Sirius** — *Rethinking Analytical Processing in the GPU Era*. [CIDR 2026](https://www.cidrdb.org/cidr2026/papers/p12-yogatama.pdf).
4. **OBELISK** — *Efficient Offline Query Planning with Bayesian Optimization-Informed Language Model Reasoning*. [PVLDB 19(7), 2026](https://www.vldb.org/pvldb/vol19/p1674-pan.pdf).
5. **APQO** — *An Adaptive Framework for Parametric Query Optimization*. [PACMMOD 3(6) / SIGMOD 2026](https://doi.org/10.1145/3769761).
6. **O3-LSM** — *Maximizing Disaggregated LSM Write Performance via Three-Layer Offloading*. [SIGMOD 2026](https://doi.org/10.1145/3802093) ([PDF](https://cs.purdue.edu/homes/csjgwang/pubs/SIGMOD26_O3LSM.pdf)).
7. **HATS** — *Holistic and Automated Task Scheduling for Distributed LSM-tree-based Storage*. [FAST 2026](https://www.usenix.org/system/files/fast26-ren.pdf).
8. **LogDrive** — *The LogDrive: Composable Durability for Cloud-Based Shared Logs*. [OSDI 2026](https://www.usenix.org/system/files/osdi26-vickers.pdf).
9. **Mantle** — *Efficient Hierarchical Metadata Management for Cloud Object Storage Services*. [SOSP 2025](https://doi.org/10.1145/3731569.3764824).
10. **Enzyme** — *Incremental View Maintenance for Data Engineering*. [SIGMOD 2026 Industry](https://arxiv.org/abs/2603.27775).
11. **WAIR** — *Workload-Aware Incremental Reclustering in Cloud Data Warehouses*. [SIGMOD 2026](https://doi.org/10.1145/3802127) ([HTML](https://arxiv.org/html/2602.23289v2)).
12. **AutoLiquid** — *Autonomic Data Layout Optimization for the Databricks Lakehouse*. [VLDB 2026 Industry](https://www.vldb.org/pvldb/vol19/p4023-liang.pdf).
13. **Semirings IVM** — *The Role of Semirings in Incremental View Maintenance*. [arXiv:2606.07795v2](https://arxiv.org/abs/2606.07795v2) (2026-09-12).
14. **对照文献** — Lehner & Neumann, *The Data World Is Not Flat: Efficient Factorized Execution for Relational Systems* ([PVLDB 19(11), 2026](https://www.vldb.org/pvldb/vol19/p3006-lehner.pdf))；*GenJoin: Conditional Generative Plan-to-Plan Query Optimizer that Learns from Subplan Hints* ([arXiv:2411.04525](https://arxiv.org/abs/2411.04525), SIGMOD 2026)；*Porting Balsa to DuckDB* ([AIDB@VLDB 2026 Workshop](https://vldb.org/2026/Workshops/VLDB-Workshops-2026/AIDB/aidb26_9.pdf)).

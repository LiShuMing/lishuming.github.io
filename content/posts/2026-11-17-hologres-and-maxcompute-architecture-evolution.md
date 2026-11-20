---
title: "从 HSAP 到 AI-Native Lakehouse：Hologres 与 MaxCompute 架构演化与设计抉择"
slug: "hologres-and-maxcompute-architecture-evolution"
date: 2026-11-17T23:10:00+08:00
lastmod: 2026-11-17T23:10:00+08:00
categories:
  - 数据库
  - 分布式系统
tags:
  - Hologres
  - MaxCompute
  - 湖仓一体
  - 查询优化器
  - 向量化执行
  - 多模态检索
description: "从在线服务、交互分析与 EB 级批处理之间的物理冲突出发，梳理 Hologres（1.0 到 4.0 HSAP 2.0）与 MaxCompute（Fuxi/DAG 2.0 到 MaxQA/Delta Live MV/LOAM）的技术演化路径，拆解存算分离、混合负载调度、学习型优化器与多模态检索背后的关键取舍。"
draft: false
toc: true
math: true
---

设想一个典型的数据平台业务：上游每秒涌入几十万条用户行为事件与订单变更，下游同时挂着三类完全相反的负载——推荐服务需要按主键在几毫秒内查出用户画像特征（每秒 \(10^5\) 级 QPS）；运营看板要做多维钻取和 RAG 知识库检索（亚秒级返回带标量权限过滤的向量与全文混合召回）；而每天凌晨还有离线任务把百 TB 级全量历史日志与商品表做大宽表关联，顺便给大模型微调清洗多模态语料。

在早期的技术栈里，这三类需求几乎必然把架构拆成四套系统：KV 点查进 HBase 或 Redis，搜索进 Elasticsearch，交互报表进 ClickHouse，海量加工丢给 Hive 或 ODPS。维护多份存储不仅带来成倍的硬件账单，更棘手的是同步链路的延迟与一致性裂缝——刚写入的新样本在向量库里搜得到、回表查结构化属性却查不到，或者一个全量重刷把线上点查拖到超时。

如果强行把它们塞进同一套引擎，最朴素的设计会立刻在五个正交维度上同时撞墙：

1. **吞吐与容错半径的冲突**：扫 500 TB 数据跑两小时的大 Shuffle，只要万分之一概率的节点宕机发生，如果像交互式 MPP 引擎那样整条查询重跑，作业几乎永远跑不完；反过来，如果每一步 Stage 都落盘做局部重试，在线点查的毫秒级延迟就无从谈起。
2. **并发与线程模型的冲突**：传统“每查询一线程（Thread-per-Query）”在存算分离架构下遇到远程 I/O 就会产生大量阻塞；高并发点查与重型扫描混跑时，少数复杂聚合查询会迅速占满工作线程池，造成队头阻塞（Head-of-Line Blocking）。
3. **优化器编译成本的冲突**：一套全功能的基于代价优化器（Cost-Based Optimizer, CBO）做逻辑重写、拿统计信息、搜索物理计划，动辄花掉几毫秒到几十毫秒 CPU。这对跑 10 秒的报表微不足道，却足以把耗时 0.3 毫秒的主键点查慢上百倍。
4. **存储物理布局的冲突（RUM 猜想）**：纯列存加块级压缩最省空间、扫描最快，但改写或删除单行要重写整块；纯行存适合点查和高频更新，做宽表十列聚合却会产生几十倍的 I/O 放大。
5. **多模态检索的过滤先后困境**：结构化过滤加向量近似最近邻搜索（Filtered ANN）如果在两个孤立进程间拼接，先搜向量再回表过滤容易在遇到高选择率过滤条件时凑不够 Top-K（空集问题），先全量过滤再暴力算距离又会退化成全表扫。

过去六年里，阿里云生态内的两款核心自研引擎 **Hologres** 与 **MaxCompute** 分别从光谱的两端出发去破解上述约束：Hologres 从**在线服务与分析一体化（Hybrid Serving/Analytical Processing, HSAP）**向右扩展至实时湖仓与多模态检索（4.0 阶段）；MaxCompute 则从 **EB 级存算分离离线大仓**向左进化出分钟级增量计算、交互式加速与生产级学习型优化器。随着 DLF 统一元数据与零拷贝通道的打通，两者呈现出非常清晰的“双内核相向演进”格局。

![Hologres 与 MaxCompute 双引擎架构演化与湖仓协同边界](/images/diagrams/db-hologres-maxcompute-bridge.svg)

---

## 一、Hologres：如何在一套引擎里同时扛住 \(10^5\) QPS 点查与 PB 级分析？

Hologres 的设计基石发表于 VLDB 2020（*Alibaba Hologres: A Cloud-Native Service for Hybrid Serving/Analytical Processing*），并在 3.0 与 4.0 版本中先后补齐了实时湖仓（Dynamic Table / Paimon 协同 / Serverless）与多模态检索（HSAP 2.0）。虽然它在接入层兼容 PostgreSQL 协议与部分系统表生态，但真正决定其行为边界的是全 C++ 自研的优化器、调度框架与存储内核。

### 1.1 双路径优化：HQO 与 Fixed Plan 的分流

Hologres 在无状态前端（Frontend, FE）接收到 SQL 后，并不会让所有请求都走同一条编译路径。

面对复杂的多维聚合、多表关联或混合检索，SQL 进入分布式查询优化器 **HQO（Hologres Query Optimizer）**。HQO 采用类似 Cascades / Orca 的自顶向下代价搜索框架，结合分片裁剪（Shard Pruning）、动态过滤（Runtime Filter）和基于 **Table Group** 的共置关联（Colocate Join——同组表按相同分发键哈希分片，物理落在同一 Worker 上从而跳过网络 Shuffle），把 SQL 切分为由 Read / Write / Query Fragment 组成的分布式有向无环图（DAG）。

但遇到高频主键点查（`SELECT ... WHERE pk = ?`）或单行/小批 `INSERT ... ON CONFLICT` 时，走完整的统计信息拉取与代价枚举本身就是巨大的性能毒药。Hologres 为此开辟了 **Fixed Plan** 短路径：前端识别出符合固定点查/点写模式的 SQL 后直接绕过 HQO，按主键哈希算出目标分片（Shard）位置，以极轻量的 RPC 直达存储引擎。正是这道分流阀，让同一个接入层既能做深度 CBO 规划，也能把单键点查和写入推进亚毫秒级、支撑十万级以上的在线 QPS。

### 1.2 HOS 与 Execution Context：存算分离下的混合负载调度

把存储放在远端分布式文件系统 **盘古（Pangu）** 之后，计算节点（HoloWorker）每次 Cache Miss 都要跨越网络发起远端读取。如果直接沿用操作系统线程绑定查询的模型，一旦百并发分析扫盘加上千并发点查涌入，内核态线程上下文切换（Context Switch）会立刻吃光 CPU。

Hologres 的调度底座 **HOS（Hologres Operating Scheduler）** 在系统线程之上引入了一层类似无栈协程的轻量级资源抽象——**Execution Context（执行上下文, EC）**：

- **全异步协作让出**：读写请求被拆解成细粒度异步工作单元并挂载到 EC 上执行。每当算子发起远端盘古读取或跨节点 Shuffle 等待网络包时，EC 主动让出物理线程，切换开销几乎为零，从而把存储计算分离的网络延迟掩盖在大量并发任务流水线之中。
- **物理线程池上的软隔离**：HOS 通过调度组（Scheduling Groups）为低延迟在线点查与重型分析分配不同的时间片配额与优先级，防止后台突发的全表扫把在线 Serving 请求饿死。

而在执行引擎本身，主力向量化引擎 **HQE** 在随后的 **QE V2** 迭代中解决了一个分布式 MPP 架构的常见痛点：**计算并行度强绑定物理分片（Shard）数**。过去如果某张历史表只切了 16 个 Shard，哪怕集群扩容到 128 核，单表复杂聚合也只能跑 16 路并行，剩下算力只能干瞪眼。QE V2 重构了算子流水线与文件内并行扫描（Parallel File Scan），允许引擎在不增加额外网络 Shuffle 的前提下，动态拉高 Scan、Hash Join 与 Aggregate 等 CPU 密集型算子的内部并行度，消除“大实例小分片”的木桶效应。

### 1.3 存储、MVCC 与实时可变的代价

在盘古之上，Hologres 把同一 Table Group 内的表水平切分为自包含的 **Shard**，每个 Shard 内部以 **Tablet** 组织数据，并通过日志结构合并树（LSM-like）机制实现实时写入与多版本并发控制（MVCC）：

1. **写入即刻可见**：写入请求先追加到盘古上的预写日志（WAL）确保持久化，随即写进 Worker 内存中的 **MemTable** 便向客户端返回成功，实现亚秒级数据可见性。MemTable 写满后异步刷盘为不可变列存或行存文件，再由后台线程做 Compaction 合并。
2. **无锁快照读与逻辑删除**：Tablet 采用单写者多读者（Single-Writer, Multi-Reader）与 MVCC 快照读，分析扫描和并发写入完全互不阻塞。高频更新与删除不原地改写大文件，而是通过 **DeleteMap（墓碑位图）** 标记旧行失效，在后台 Compaction 时物理回收。
3. **行存、列存与行列共存的权衡**：建表时可通过 `orientation` 选择纯列存（高压缩、支持稀疏索引与 Roaring Bitmap 人群圈选）、纯行存（主键点查避免读取几十个列文件的放大），或**行列共存**。行列共存从同一份 WAL 事务版本号派生出两份物理表示并保持事务级同步可见，代价是约双倍的存储空间以及后台 Flush/Compaction 同步开销（官方文档据此建议行列共存表控制在 300 列以内）。

### 1.4 从 3.0 实时湖仓到 4.0 多模态检索：Dynamic Table、Serverless 与 HGraph

2024 至 2026 年间，Hologres 的演化重心明显从“单纯加速内表”转向**简化实时数仓链路**与**原生融合 AI 多模态**：

- **用声明式 Dynamic Table 取代繁琐流计算**：过去基于 Hologres Binlog + Flink 搭建实时数仓虽然省掉了 Kafka，仍需维护庞大的 Flink RocksDB 状态。3.0 引入的 **Dynamic Table** 允许直接用一条标准 SQL 声明衍生表和刷新策略；3.1 版本默认将基表变更捕获从 Binlog 切换为无额外存储开销的原生 **Stream 模式**，并把增量刷新作业默认卸载至 **Serverless 资源池**运行；4.0 版本进一步延伸出 **External Dynamic Table**，直接对湖上 **Apache Paimon** 数据加工并写回湖表。
- **三级弹性与 Query Queue**：在早期按实例扩缩容的基础上，Hologres 先通过 **Virtual Warehouse（计算组）** 实现“一份盘古存储挂载多个主/只读计算组”的读写与业务物理隔离；3.x–4.0 阶段进一步落地 **Serverless 实例**与 **Query Queue**，当拦截到大查询或触发动态表后台刷新时，自动路由至最高可弹性至 **512 CU** 的共享 Serverless Computing 资源池按查询计费执行，保住核心主实例的 P99 延迟。
- **HGraph + RabitQ + Tantivy（HSAP 2.0 多模态内核）**：为了避免在 RAG 与智能检索场景下同时维护向量库与 Elasticsearch，Hologres 4.0 将自研层次图向量索引 **HGraph**、高压缩二值量化 **RabitQ** 与基于 Rust 全文引擎 **Tantivy** 的 BM25 倒排索引直接做进存储和执行内核。针对 HNSW 全内存索引在百亿规模下内存成本失控的问题，HGraph 采用“**内存驻留低精度导航图 + 盘古落盘高精度原始向量**”的冷热分层（内存占用最高减少约 80%），配合点聚合图（PAG）减少分布式存储随机 I/O，并用 Elias-Fano 编码把图邻接表压至原体积约 1/3；而 **RabitQ** 将高维归一化浮点向量投影至超立方体顶点（低至 1-bit/维），把浮点内积转化为 CPU SIMD 位运算。因为标量索引、倒排索引与图索引共享同一份存储与 **HQO 优化器**，引擎可以根据过滤条件的选择率统一估算代价，避免外置向量库在 Filtered ANN 上的精度或性能塌缩。

---

## 二、MaxCompute：EB 级批处理底座的“近实时化”与学习型优化突围

如果说 Hologres 是在毫秒级延迟的约束下向大模型和湖仓伸手，MaxCompute（原 ODPS）面对的则是另一套极端物理条件：**单集群数万台节点、超十万个多租户企业项目、单作业动辄扫过 PB 级数据与百 TB 级 Shuffle**。在这个量级上，任何一点资源碎片或执行计划劣化都会被放大成数千核时的直接成本。

### 2.1 执行与调度底座：Fuxi、DAG 2.0 Bubble 模式与 MaxQA

MaxCompute 最早用自研的 **Fuxi（伏羲，VLDB 2014）** 分布式调度器替代早期 Hadoop 体系的扩展性瓶颈，通过增量资源协议与 Task 级容错在常态化的机器故障中保住长耗时批作业（关于 Fuxi 与现代开源调度器的源码级对应关系，可见姊妹篇 [从 Fuxi 到现代开源调度器]({{< relref "2026-11-17-fuxi-and-open-source-schedulers-evolution.md" >}})）。但随着负载向交互式和异构混部演进，纯离线落盘的执行模式暴露了明显短板，驱动执行层完成了三次跃迁：

1. **DAG 2.0 的 Bubble Execution Mode（气泡执行模式）**：
   纯离线模式在每个 Stage 之间把中间结果写盘，容错粒度细、资源利用率高，但磁盘和调度延迟大；而纯 MPP 流式全包模式（Gang Scheduling）一次性拉起全部 Stage 并全走内存管道，延迟虽低，只要遇到一个慢节点或数据倾斜就会空占整张计算图资源，且任意实例故障都要整图重来。
   DAG 2.0 给出的折中方案是**气泡模式（Bubble）**：编译器将大 DAG 切分成若干受控规模的 **Bubble 子图**，Bubble 内部采用内存/网络流水线紧耦合调度加速，Bubble 边界则通过持久化 Shuffle 落盘切开故障域与生命周期。公开的 TPC-H 1TB 评测显示，Bubble 模式比纯离线模式**提速 200% 而资源开销仅增加 17%**；相比全图 Gang 模式则以 **40% 的资源消耗换取了 85% 的执行速度**。
2. **异构混部与智能配额（Fangorn & IPA）**：
   面向共享大集群上的混部压力，**Fangorn（VLDB 2021）** 根据运行时真实统计动态调整调度粒度；**IPA（VLDB 2022）** 则用多目标机器学习模型在亚秒级内决策作业的分片数、容器 CPU/内存配比与放置策略，压低多租户混部下的尾延迟和算力成本。
3. **C++ Native 向量化与 MaxQA（MCQA 2.0）**：
   针对 BI 即席查询场景，MaxCompute 推出了隔离加速资源池 **MaxQA（MCQA 2.0）**。它在不搬迁底层盘古表数据的前提下，开辟专用计算环境并叠加**元数据、执行计划、Shuffle 中间结果与最终结果的多层本地 SSD 缓存**，配合面向延迟优化的执行计划和 C++ 代码生成/向量化引擎，把几十秒的常规离线查询压入秒级/亚秒级；超出加速池容量的超重型查询则平滑回退给离线引擎。

### 2.2 查询优化器四重奏：为什么生产云数仓必须引入 HBO、Tempura 与 LOAM？

在数据库内核的优化器板块，MaxCompute 过去几年的学术与工程成果非常具有代表性，因为它们精准踩中了“学术理论”与“十万级多租户云生产环境”之间的每一道裂缝：

| 优化器层级 | 核心机制与公开文献 | 解决的真实痛点 |
| :--- | :--- | :--- |
| **经典 CBO** | 基于 Apache Calcite 扩展（Join Reorder / 动态分区裁剪） | 依赖 `ANALYZE` 统计信息做代数变换，但在多级 Join 与复杂 UDF 之后基数估计极易失真 |
| **HBO（基于历史优化）** | 自动记录周期性作业历史运行的真实行数、偏斜度与耗时，反馈给下次编译 | 云上大量 T+1 / 小时级 ETL 模板每天重复运行，昨日真实统计远比静态直方图准确 |
| **Tempura（增量代价优化）** | **VLDB 2021**：提出 **Time-Varying Relations (TVR)** 与 TIP 模型，将全量与增量维护纳入统一代价空间 | 传统增量物化视图规则僵化——当 Delta 占比过大（尤其双流 Outer Join）时，增量算比全量重算更慢。Tempura 按代价动态挑选最优全/增混合计划，支撑起生产级 **Delta Live MV** |
| **LOAM（学习型优化器）** | **SIGMOD / arXiv 2026 (`2602.07336`)**：专为云端多租户打造的 Learned Query Optimizer | 突破学术界 Neo/Bao/Balsa 无法上生产的四大限制（详述如下），在生产验证集上最高节省 **30% CPU 成本** |

**LOAM（2026）揭露了 Learned Optimizer 落地的真实门槛**。学术界很多学习型优化器之所以一上生产就翻车，背后卡着四个结构性死结：

1. **执行环境剧烈波动**：在多租户共享集群上，同一条执行计划在不同负载时段跑出的耗时方差极大。LOAM 采用 **Environment-aware Cost Learning**，在训练时显式把集群运行环境特征编入模型，在线推理面对未知未来环境时则给出带理论上界的鲁棒平滑预测。
2. **输入统计信息大面积缺失**：云上大量临时表、外部表和 UDF 中间结果根本没有最新直方图。LOAM 引入 **Statistics-free Plan Encoding**，不强依赖目录统计信息，而是通过算子语义结构与历史执行轨迹隐式推断数据分布。
3. **严禁在线试错探索**：工业界绝不允许通过在线强化学习（RL Exploration）拿真实客户作业去“试跑烂计划”并导致账单暴涨。LOAM 用 **Domain Adaptation（域自适应）** 增强模型对未见在线计划的泛化能力，完全砍掉了高风险的在线重训探索环路。
4. **不同项目收益高度不均**：面对超 10 万个企业项目，LOAM 增加了一个轻量级 **Project Selector**，自动甄别工作负载匹配、潜收益高的项目开启干预，对无把握或已近最优的作业直接放行给原生优化器，死守“零 Plan Regression”红线。

### 2.3 存储、开放湖仓与 AI 原生：Delta Table、Object Table 与 Storage API

在存储与生态边界上，MaxCompute 在 2024–2026 年完成了“闭源自保”与“对外开放”的双向改造：

- **从 AliORC 到自研 Delta Table + Delta Live MV**：列存格式经历 CFile1 $\rightarrow$ CFile2 $\rightarrow$ **AliORC**（结合约 6.5 倍平均压缩比、自适应字典与热温冷分层存储）后，引入基于 Merge-On-Read（`BaseFile + DeltaFile`）的自研 **Delta Table**（支持主键 Upsert/Delete、Read Committed 事务与 Time Travel 时间旅行），并由托管的 **Storage Service** 自动做小文件合并与聚簇排序。配合由 Tempura 演化而来的 **Delta Live MV（动态物化视图）**，系统可自动捕获源 Delta 表 CDC 增量，仅重算增量部分，支撑双流 Join/Aggregate/Window 等算子的分钟级近实时数仓加工。
- **OpenLake 与 Storage API（打破闭源接口）**：通过 **DLF（Data Lake Formation）** 统一纳管 OSS 上的 Paimon / Iceberg / Parquet 外部表；同时对外开放基于 **Apache Arrow** 的高性能列式 **Storage API**，在服务端就地完成列裁剪与谓词下推后再高吞吐吐给 Spark、Flink、Hologres 或 StarRocks，取代早期重序列化开销的 HTTP Tunnel。
- **MaxFrame 与 Object Table（AI-Native 底座）**：分布式的 Python 计算框架 **MaxFrame** 彻底接替了早期需要单独维护集群的 Mars，直接将 Pandas/Scikit-learn 计算图提交给 MaxCompute Serverless 池执行。而面对自动驾驶和具身智能场景下 OSS 里海量的图片、视频与 PDF，传统外部表既不能感知对象大小做均匀切分，每次查询远程 List 目录又极慢；**Object Table（对象表）** 将 OSS 非结构化对象的路径、大小、修改时间等元信息**版本化缓存为结构化元数据表**，让 SQL 优化器能直接对非结构化文件做谓词下推与分布式并发切分，并调度自定义容器镜像 UDF 或内置 `AI Function`、`VECTOR_SEARCH` 完成清洗与特征向量化。

---

## 三、关键设计抉择的底层权衡：两套引擎为什么不会合成一个内核？

看完两者的能力图谱，一个很自然的问题是：Hologres 在做动态表、湖表镜像与 Serverless 计算，MaxCompute 也在做 Delta Live MV、MaxQA 秒级交互查询与向量函数——它们未来会合并成一套引擎吗？

从系统底层物理约束来看，**用户心智与湖仓元数据（DLF）正在加速统一，但底层运行时内核几乎不可能合二为一**。原因在于双方在四条底线问题上做出了截然相反的架构选择：

| 核心设计权衡 | Hologres 的取舍（低延迟与高并发优先） | MaxCompute 的取舍（吞吐、容错与 TCO 优先） | 不可合并的物理根源 |
| :--- | :--- | :--- | :--- |
| **存算分离延迟掩盖** | Worker 软亲和 + 本地 NVMe Block Cache + HOS 协程全异步切换 | AliORC 高压缩减网络量 + 聚簇表裁剪 + Bubble 子图内存复用（MaxQA 加局部 SSD Cache） | 为微秒级点查设计的内存结构与固定路由，无法承受单查询扫 500 TB 的内存膨胀 |
| **容错模型** | MPP Pipeline 快速失败重试（查询级秒级重跑） | Fuxi Task / Bubble 边界落盘容错（局部重算） | 万节点跑 2 小时的超大 Shuffle 若采用 MPP 全图重跑，在大规模节点故障率下将无法收敛 |
| **可变更新代价** | WAL + 常驻 MemTable + DeleteMap + 可选行列共存 | 面向批/微批追加，Merge-on-Read 异步后台 Compaction | 让冷历史 PB 级归档表常驻内存索引和行存副本是巨大的硬件浪费 |
| **查询优化策略** | Fixed Plan 零开销短路径 + HQO 毫秒级搜索 | CBO + HBO 历史反馈 + Tempura 增量代价 + LOAM 模型推理 | PB 级批作业值得花数百毫秒甚至数秒找更优计划（节省 30% CPU）；在线 Serving 连 2 毫秒编译税都付不起 |

这也解释了当前最务实的**湖仓双内核协同模式**和两条清晰的迁移边界：
- **何时必须进入 Hologres 内表？** 需要上千至十万级 QPS、P99 小于 10 ms 的主键点查（依赖行存与 Fixed Plan）；高频核心模型需要 **Table Group Colocate Join** 消除 Shuffle；要求写入后亚秒级立即可见；或需要 **HGraph + Tantivy** 在线混合检索。
- **何时留在 MaxCompute 或开放湖（Paimon / OSS）通过直读加速？** TB/PB 级全量离线清洗与大宽表 Shuffle；大批量多模态非结构化数据离线预处理（Object Table + MaxFrame）；低频温冷历史回溯分析——借助 **C++ Native 直读 2.0** 与 **Arrow Storage API**，完全可以做到零数据搬迁下的交互式探索，无需为冷数据支付双份存储与常驻计算资源账单。

---

> **参考资料与公开信息基线**
> - **Hologres 核心论文与文档**：Xiaowei Jiang et al., *Alibaba Hologres: A Cloud-Native Service for Hybrid Serving/Analytical Processing* (PVLDB 2020, 13(12): 3272–3284) · 阿里云 Hologres 官方技术文档（V3.0/V3.1/V3.2/V4.0 发布说明、Dynamic Table、Serverless Computing / Query Queue、HGraph/RabitQ 向量索引与 Tantivy 全文检索文档）。
> - **MaxCompute 核心论文与文档**：Zhuo Zhang et al., *Fuxi: a Fault-Tolerant Resource Management and Job Scheduling System at Internet Scale* (PVLDB 2014) · *Fangorn: Adaptive Execution Framework for Heterogeneous Workloads on Shared Clusters* (PVLDB 2021) · Zuozhi Wang et al., *Tempura: A General Cost-Based Optimizer Framework for Incremental Data Processing* (PVLDB 2021) · Chenghao Lyu et al., *Fine-Grained Modeling and Optimization for Intelligent Resource Configuration in Big Data Processing* (PVLDB 2022) · Lianggui Weng et al., *Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions* (SIGMOD / arXiv:2602.07336, 2026) · 阿里云 MaxCompute 官方技术文档（AliORC、DAG 2.0 Bubble 模式、Delta Table 与 Delta Live MV、MaxQA、Object Table、Storage API 及开源 `aliyun-odps-python-sdk` / `alibabacloud-odps-maxframe-client`）。

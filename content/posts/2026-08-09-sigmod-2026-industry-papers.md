---
title: "SIGMOD 2026 Industry Papers"
date: 2026-08-09T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
categories:
  - 数据库
tags:
  - SIGMOD
  - 数据库
  - 论文
description: "SIGMOD 2026 工业界 29 篇论文全景阅读笔记：从云原生分层存储、分布式 OLTP、AISQL 与学习型优化器，到弹性 Shuffle、并行共识与 Code-to-SQL 可观测闭环，拆解生产约束下的真实系统取舍。"
draft: false
toc: true
math: true
---

> 本系列第 2/3 篇 · 论文雷达 · [上一篇]({{< relref "2026-09-19-database-research-frontiers.md" >}}) · [下一篇]({{< relref "2026-08-23-database-paper-reading-notes.md" >}})

[SIGMOD 2026](https://2026.sigmod.org/) 于 2026 年 5 月 31 日至 6 月 5 日在印度班加罗尔（Bengaluru）举行。每年读顶会论文时，我都会把工业界赛道（Industry Track）单独拎出来通读一遍——学术论文擅长在受控假设下把单一算子或算法推向极致，而工业界论文最迷人的地方，在于它不得不摊开**生产环境里的守恒定律**：当你把存储拆成冷热多层、把单机事务拆成跨节点分片、或者把大模型塞进 SQL 算子时，被挪走的那部分开销究竟转移到了哪条链路，尾延迟（Tail Latency）和云账单靠什么兜底？

今年 Industry Track 共录用 **29 篇**论文，来自 AWS、Microsoft、Google、Snowflake、Databricks、Oracle、IBM、Adobe、Dynatrace、Neo4j，以及阿里巴巴、字节跳动、腾讯、美团等一线工程团队。如果按公司名字罗列，很容易看成一盘散沙；但只要顺着现代数据平台的数据流与控制流把它们摆进同一张系统架构图，就会发现这 29 篇工作全都围绕着七个紧耦合的演化界面展开：

![SIGMOD 2026 工业界 29 篇论文系统版图：生产级数据栈的七大演进界面](/images/diagrams/aug-dbpaper-sigmod2026-landscape.svg)

| **主题方向** | **篇数** | **代表系统与论文** | **核心工程矛盾** |
|---|---:|---|---|
| 1. 云原生数据库、分层存储与扩展 OLTP | 7 | CloudJump III、Aurora Limitless、Hyperscale、TDSQL-Boundless、Bigtable 20 年 | 打破单机容量与单写者上限后，如何压住远端页延迟、租约风暴与跨分片提交代价 |
| 2. 查询引擎、云数仓、硬件加速与 AISQL | 6 | LOAM、Cortex AISQL、ByteHouse、Enzyme、Fabric Bitmap / CoddSpeed | 在缺统计、多租户抖动与昂贵 LLM 推理约束下，守住计划稳定性与增量刷新总账 |
| 3. 推荐特征计算、训推存储与弹性 Shuffle | 3 | TokaDB、NebulaSQL、FuxiShuffle | 弥合离线大吞吐训练与在线低延迟特征服务裂痕，让中间数据交换具备细粒度容错 |
| 4. 一体化图引擎、并行共识与受监管账本 | 4 | Poseidon、G2+D、RIOT、Fabric-X | 兼顾图遍历局部性与并发更新，打破单 Leader 全序复制对无冲突事务的串行束缚 |
| 5. 分布式向量检索、企业 AI 助手与交互评测 | 3 | LindormVector、Adobe Enterprise AI、ConDABench | 把向量检索融入多模过滤与权限治理，评估多轮模糊意图下的真实分析能力 |
| 6. Code-to-SQL 归因、AIOps 诊断与迁移 | 3 | SQLens、美团根因 SQL 排序、JSON-to-Duality 迁移 | 穿透 ORM 抽象找回代码与慢 SQL 的因果链，在全实例资源争用中定位真凶 |
| 7. 解耦内存缓存、接入池化与时序压缩 | 3 | LakeMem、CLAPS、Dynatrace 浮点压缩 | 区分共享基表与私有中间态语义，用负载感知池化与轻量无损压缩削减冗余水位 |

下面按这七组生产主线逐一拆解每篇论文的下载入口、核心机制与阅读抓手。

---

## 1. Production Database / Cloud-Native DB / Storage

如果用最朴素的方式给生产数据库扩容，无非两条路：存储装不下就挂更便宜的远程盘，单机写不动就按哈希切分片。但一旦动手拆分，三个隐性代价就会立刻冒头：**操作系统块层根本不知道哪个 8KB 页马上还要被访问、上百万个共识分片光是续租心跳就能把 CPU 打满、跨分片事务一旦引入中心协调器又会变成新的单点瓶颈。** 本组 7 篇论文正是各大厂针对这些拆解副作用交出的工程答卷。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 1 | **ByteGraph-Dione: An Adaptive Dual-Format Graph Engine with Hotspot Awareness and Transaction Efficiency for Production-Scale Workloads** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803073) / [ACM DOI](https://dl.acm.org/doi/10.1145/3788853.3803073) | 字节跳动面向超大规模生产负载的双格式（Dual-Format）图引擎。图系统长期卡在一个结构性矛盾上：紧凑邻接数组适合高速多跳遍历，但高频边增删改极易引发锁争用与重写放大；可变动态结构写入友好，读局部性又差。从议题定位看，精读全文时最值得重点追问三件事：两种物理格式各自承接哪类读写路径？在线热点感知与后台格式转换如何控制切换抖动？图遍历快照与并发事务更新之间怎样定义冲突边界？ |
| 9 | **Scalable Leader Leases For Multi Consensus Groups in CockroachDB** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803081) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803081) | CockroachDB 剖析海量 Raft Range（多共识组）下的 Leader Lease 扩展瓶颈。为了免去每次本地读都走一轮多数派 Quorum，分布式数据库普遍依赖租约（Lease）由 Leaseholder 直接安全服务读请求；但当单节点承载成千上万个 Range 时，逐个 Range 独立续租和迁移就会演变成控制面风暴。这篇论文直击 **Read Lease 生命周期、Range 负载均衡迁移、故障切换安全性与防陈旧读（Stale Read Safety）** 的工程细节，做 Multi-Raft 或分布式 KV 的工程师不容错过。 |
| 11 | **Scalable and Resilient Storage Tier for Azure SQL Hyperscale** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803083) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803083) | 微软系统复盘 Azure SQL Hyperscale（早期代号 Socrates）存储底座的可扩展与韧性演进。它把传统单机 SQL Server 紧耦合的页面缓冲、日志刷盘与备份恢复彻底拆解为独立的日志服务（Log Service）、弹性分片页服务（Page Server）与远端持久存储。阅读重点在于生产规模化之后如何压平 Page Server 补页尾延迟、加速副本追赶（Catch-up）与崩溃恢复，并在局部节点失联时守住 SLA。 |
| 12 | **CloudJump III: Optimizing Cloud Databases for Tiered Storage** | [作者版 PDF](https://desert0616.github.io/pdf/CloudjumpIII.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803084) | 阿里云 CloudJump 第三代架构，核心主张非常犀利：**云数据库的冷热分层绝不能做在语义盲目的块设备或文件系统层，而必须深植进数据库内核的 Buffer Manager。** 它在页面淘汰（Eviction）与刷盘（Flush）控制点引入引擎感知的数据放置策略，综合页面类型（索引内节点 vs 数据叶页）、年龄、表身份及恢复/快照语义，在本地 NVMe、远程块存储与对象存储（Object Storage）之间动态调度，从而大幅压缩昂贵快层容量占用，同时保住接近全本地 SSD 的吞吐与平稳尾延迟。 |
| 18 | **Aurora PostgreSQL Limitless Database: Building a Highly Scalable OLTP Database** | [作者版 PDF](https://software.imdea.org/~gotsman/papers/limitless-sigmod26.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803089) | AWS Aurora PostgreSQL Limitless 的横向扩展（Scale-out）架构论文。经典 Aurora 虽然解耦了存储，但仍受困于单 Writer 计算上限；Limitless 在保留原汁原味 PostgreSQL 生态兼容性的前提下，引入无提交状态的 Router Fleet 与底层各自挂载 Aurora 共享存储的 Shard Fleet，依托有界时钟（Amazon Time Sync）实现标量时间戳 MVCC，并用参与分片自举的 Lead-Shard 非阻塞 2PC 避开了全局中心事务管理器，向百万级 TPS 与 PB 级事务规模演进。 |
| 19 | **TDSQL-Boundless: A Distributed Database System for Large-scale Heterogeneous Multi-Table Workloads** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803090) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803090) | 腾讯 TDSQL 面向大规模异构多表混合负载的分布式数据库实践。真实企业核心库很少是教科书里那种“单主键点查”的规整模型，往往同时混杂着高频短事务、跨多表复杂关联与倾斜热点。从公开定位出发，阅读全文时可以顺着“数据自动切分与共置（Collocation）路由 → 跨分片分布式事务开销 → 多表复杂 Join 下推 → 多业务资源隔离”这条链路检验其内核取舍。 |
| 24 | **Twenty Years of Bigtable** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803095) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803095) | Google Bigtable 二十年工业演进长卷。2006 年的原始 Bigtable 奠定了 Tablet 切分、LSM-Tree + SSTable 与底层分布式文件系统协同的经典范式；二十年间硬件从机械盘换成了闪存与高速网络，工作负载也从网页索引一路扩展到全球多租户实时服务。阅读这类跨时代复盘，最宝贵的视角是看它**如何在维持上层宽表契约稳定的同时，在内部持续重写元数据管理、Compaction 调度、跨地域复制与多租户隔离边界。** |

---

## 2. Query Engine / OLAP / Data Warehouse / AISQL

在分析型引擎里，单核向量化扫描的红利已经接近饱和，真正的生产账单开始向两头转移：一头是**计划与维护成本**——缺统计信息或集群资源抖动导致选错计划，或者每天全量重算 ETL 管道烧掉成倍算力；另一头是**异构算子成本**——把硬件加速器或大模型推理原语直接嵌进 SQL 后，PCIe 数据搬运和每秒几美分的 Token 账单随时可能吞掉全部优化收益。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 2 | **Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions** | [arXiv PDF](https://arxiv.org/pdf/2602.07336) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803074) | 强烈推荐查询优化器工程师精读的阿里巴巴 MaxCompute 学习型优化器 **LOAM**。论文坦诚摊开了学术界 LQO 在超大规模共享数仓落地的四堵墙：五千台规模资源池动态波动导致同计划耗时方差极大、海量表缺失新鲜统计信息、线上绝不允许为攒训练样本而试跑高危烂计划、十万级 Project 收益分化悬殊。LOAM 用不依赖显式直方图的算子语义编码、Stage 运行环境建模、梯度反转领域自适应（免在线试跑评估候选）以及事前 Project ROI 筛选器打通了生产闭环，在高潜项目回放中最高节省约 30% CPU 开销。 |
| 3 | **Bitmap Filtering in the Fabric Data Warehouse** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803075) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803075) | 微软 Fabric 云数仓中的位图运行时过滤（Bitmap Filtering）工程化剖析。原理上看，在 Join 的 Build 侧构造紧凑位图并尽早下推到 Probe 侧列存扫描，是削减网络 Shuffle 与无效解压的利器；但在分布式 MPP 引擎里，魔鬼全在细节：优化器怎样预判过滤率以防“构造与广播位图的开销倒挂”？位图如何跨交换算子低延迟汇聚并与 SIMD 向量化扫描无缝啮合？这篇给出了完整的工业界实现拆解。 |
| 5 | **CoddSpeed: Hardware Accelerated Query Processing in Microsoft Fabric** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803077) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803077) | 微软 Fabric 的硬件加速查询执行系统。在生产多租户云数仓里引入异构硬件加速，最怕掉进“单算子跑分飞快、端到端查询更慢”的陷阱。阅读重点在于系统层面的四道护栏：优化器代价模型如何计入 Host–Device 内存搬运税、哪些算子与数据类型允许部分卸载、显存或加速器饱和时如何无感回退（Fallback）到常规 CPU 路径，以及多租户共享硬件时的配额隔离。 |
| 8 | **ByteHouse: ByteDance’s Cloud-Native Data Warehouse for Real-Time Multimodal Data Analytics** | [arXiv PDF](https://arxiv.org/pdf/2602.08226) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803080) | 字节跳动云原生实时多模态数仓 **ByteHouse** 全景架构。面对结构化标量、高维向量与多模态检索日益频繁的同查询混跑需求，ByteHouse 构建了统一表引擎与物理一致布局，底层配有集群级 SSD 缓存 **CrossCache** 与虚拟文件系统 **NexusFS**，执行层打通批处理与增量计算模式，针对分层向量索引定制了运行时过滤（Runtime Filtering），并在优化器侧融合历史执行轨迹与 AI 辅助计划选择。 |
| 22 | **Cortex AISQL: A Production SQL Engine for Unstructured Data** | [arXiv PDF](https://arxiv.org/pdf/2511.07663) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803093) | Snowflake 生产级语义 SQL 引擎 **Cortex AISQL**，今年最值得细读的 AI + DB 系统论文之一。一旦把 LLM 过滤、提取和关联提升为原生 SQL 算子，传统优化器“I/O 最贵、表达式求值近乎免费”的假设瞬间倒塌——一次大模型调用比普通谓词贵出好几个数量级，且选择率在编译期完全未知。论文祭出三招破局：把 Token/推理成本纳入一等优化目标、用**自适应模型级联（Adaptive Model Cascades）**让廉价小模型先筛掉绝大多数确定样本、再通过**语义连接改写（Semantic Join Rewriting）**把原本 \(O(|L|\times|R|)\) 的两表逐对提示词调用转化为多类分类映射，在对应实验场景下取得了 2–8×、2–6× 乃至 15–70× 的显著提速。 |
| 27 | **Enzyme: Incremental View Maintenance With Spark Declarative Pipelines** | [arXiv PDF](https://arxiv.org/pdf/2603.27775) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803098) | Databricks 声明式数据管道背后的增量视图维护（IVM）引擎 **Enzyme**。企业数仓里大量 T+1 或小时级 ETL 管道每天都在把整张底表重算一遍，而手写增量合并逻辑又极易在复杂窗口、外连接或非单调聚合上写出错数 Bug。Enzyme 把物化视图刷新变成一个**基于代价的全局规划问题**：自动分析算子可增量性，在行级 Delta 合并、分区级局部重算与退回全量刷新之间算清总账，并沿管道 DAG 协同上下游策略。 |

---

## 3. Recommendation / Feature Engineering / Shuffle / ML Data Infrastructure

大规模推荐与机器学习基础设施长期忍受着两处高昂的“管道割裂税”：一是**训推数据割裂**——离线训练追求高带宽顺序吞吐历史样本，在线推理（Serving）追求毫秒级点查最新特征与 Embedding，维护两套存储既翻倍烧钱又常闹特征穿越/不一致；二是**Shuffle 中间态脆弱**——超大规模分布式作业卡在细碎随机 I/O 与单节点故障引发的连环重算上。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 6 | **TokaDB: A Unified Storage Engine for Training-Serving Data Management in Large Recommendation Models** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803078) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803078) | 字节跳动联合华中科技大学提出的推荐大模型训推一体存储引擎 **TokaDB**。要把离线海量顺序读与在线高并发低延迟读写塞进同一套引擎，核心硬仗在于物理布局与资源隔离：高频实时特征写入怎样压制写放大？训练任务大吞吐拉取快照样本时，如何保证不抢占在线 Serving 的内存与尾延迟预算？从公开议题出发，全文的快照多版本机制与冷热分层策略非常值得对照研读。 |
| 7 | **NebulaSQL: A Large-scale Feature Computation System for Online Recommendation** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803079) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803079) | 阿里巴巴面向在线推荐的大规模特征计算系统 **NebulaSQL**。推荐业务里充斥着复杂的滑动窗口、多路序列交叉与自定义算子（UDF），过去常由业务团队手写 C++ 胶水代码拼凑。NebulaSQL 将在线特征工程收拢进类 SQL 声明式编译与优化框架中，重点解决**离线/在线特征计算逻辑同源一致、有状态增量更新，以及严苛毫秒级 SLA 下的执行定制优化**。 |
| 14 | **FuxiShuffle: An Adaptive and Resilient Shuffle Service for Distributed Data Processing on Alibaba Cloud** | [arXiv PDF](https://arxiv.org/pdf/2602.22580) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803085) | 阿里云 MaxCompute/Fuxi 生产级远程 Shuffle 服务 **FuxiShuffle**。它跳出了静态“全走内存”或“全落磁盘”的非黑即白选择，把数据交换做成一套由实时遥测驱动的自适应控制面：根据预测运行时与实时内存水位动态切换内存/盘古磁盘介质，用 **Progressive Scheduling** 在上游进度跨过动态阈值时提前拉起下游边产边读，按每个数据块的重建代价按需挂载双副本备份，并在节点故障时通过版本与校验和实现**不丢弃下游已读进度的增量恢复（Incremental Recovery）**。 |

---

## 4. Graph / Consensus / Blockchain

这一组 4 篇论文分别落脚在**图计算/图挖掘**与**分布式共识/状态机复制**。看似分属两个子领域，底层搏斗的其实是两类经典的系统扩展极限：图负载挑战的是**非规则指针跳转与高阶子图枚举带来的组合中间态爆炸**；而新型共识与账本引擎挑战的则是 **Paxos/Raft 单 Leader 全序日志在无冲突并发事务面前的中心化排队瓶颈**。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 4 | **Poseidon: A OneGraph Engine** | [作者版 PDF](https://olafhartig.de/files/BebeeEtAl_SIGMOD2026_CameraReadyVersion.pdf) / [arXiv PDF](https://arxiv.org/pdf/2510.11166) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803076) | AWS Neptune Analytics 背后的核心图引擎 **Poseidon**。长期以来，属性图（LPG）与语义网（RDF）、在线图事务查询与全图分析算法往往分属不同系统。Poseidon 扩展 openCypher 打通 RDF/LPG 互操作（OneGraph），在内核层通过**无锁邻接表维护（Lock-free Adjacency List）、简洁二级索引（Succinct Indices）、分区堆内存、代价优化器统计信息与逻辑日志持久化**，把可变长路径匹配、图算法调用与动态事务更新揉进同一套存储执行底座。 |
| 15 | **G2+D: A High Performance Distributed Graph Mining System** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803086) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803086) | 字节跳动面向风控、反欺诈与社区发现场景的高性能分布式图挖掘系统。不同于只找最短路径或一跳邻居的常规遍历，子图模式挖掘（Motif / Pattern Enumeration）每向外扩展一跳，候选同构状态就可能呈指数级膨胀，且超级节点极易把单台机器的通信与内存拖垮。精读全文时，重点关注它如何协同子图对称性破缺剪枝、跨机邻接缓存压缩与动态窃取负载均衡。 |
| 21 | **Fabric-X: Scaling Hyperledger Fabric for Asset Exchange** | [IACR ePrint](https://eprint.iacr.org/2023/1717) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803092) | IBM Research 针对受监管金融资产交换重构的 **Fabric-X** 架构。换上数据库内核的眼镜来看，联盟链账本就是一台跨多信任域的确定性复制状态机（Deterministic Replicated State Machine）：传统“模拟背书 → 全局排序 → 串行验证落账”流水线在高并发资产划转下极易卡在排序带宽与状态校验锁上。Fabric-X 系统拆解了背书、排序与并行状态验证的流水线解耦，同时守住监管合规与防双花可串行化边界。 |
| 23 | **RIOT: Replicated Independently-Ordered Transactions** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803094?download=true) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803094) | Neo4j、NVIDIA 与 UC Berkeley 联合提出的去中心化事务复制协议 **RIOT**。标准 Raft/Paxos 之所以简单稳健，是因为强迫所有写入无论是否冲突都去单 Leader 领一个全局单调递增的日志槽位；一旦跨地域部署或热点分散，这种人为全序就成了吞吐枷锁。RIOT 改在有向无环图（DAG）日志条目上做去中心化协调，让互不冲突的事务独立定序、并行复制提交，在消除单点 Leader 瓶颈的同时严格保持可串行化等价语义。 |

---

## 5. Vector / Multimodal / AI Data Platform

把向量检索与大模型助手搬进真实企业生产环境后，早年围绕“纯内存静态数据集裸跑 ANN QPS”的比拼迅速退居次席。真正决定系统生死的变成了三件事：**带复杂标量过滤与实时增删改时的召回率/成本曲线、细粒度企业权限与审计隔离，以及面对人类含糊不清的数据问题时能否多轮追问对齐意图。**

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 17 | **LindormVector: A Distributed Vector Engine on a Cloud-Native Multi-Model NoSQL Database** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803088) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803088) | 阿里云云原生多模 NoSQL 数据库 Lindorm 上的分布式向量引擎。在 RAG 与海量电商/内容检索中，业务几乎从不发裸向量查询，而是要求“租户 ID + 时间窗口 + 全文关键词 BM25 + 向量相似度”联合过滤，同时底层文档还在持续实时写入。论文重点剖析如何在分布式多模底座内协同分片路由、增量向量索引更新、标量/全文/向量混合检索计划选择，以及高开销向量任务与常规 KV 点查之间的资源隔离。 |
| 13 | **Building, Serving, and Growing a Conversational AI Assistant for Enterprise** | [作者版 PDF](https://www.sumitbhatia.net/papers/sigmod26.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3801880) | Adobe 面向企业生产环境构建、服务与持续演进对话式 AI 助手的全链路工程复盘。企业级助手最怕的不是某道百科题没答上来，而是越权吐出无权限访问的内部文档，或者在产品迭代后静默出现回答质量滑坡。论文从数据平台视角详述了企业异构知识接入、行/文档级访问控制透传、线上护栏与事故预防（Incident Prevention）、离线/在线双轨评估闭环及用户反馈飞轮。 |
| 28 | **ConDABench: Interactive Evaluation of Language Models for Data Analysis** | [arXiv PDF](https://arxiv.org/pdf/2510.13835) / [Microsoft PDF](https://www.microsoft.com/en-us/research/wp-content/uploads/2026/05/3788853.3803099-1.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803099) | 微软针对交互式数据分析设计的评测基准 **ConDABench**。现有 Text-to-SQL 榜单大多假设用户一次性给出了毫无歧义的自然语言规范，且底表干净整洁；可现实里业务方的提问往往口径模糊、底表满是脏数据与隐式空值，分析师必须先查探数据再多轮追问确认。ConDABench 用多智能体工作流构造了 1,420 个多轮交互数据分析任务及配套评测底座，并揭示了一个警醒结论：**单轮解题分数更高的新模型，在需要长期多轮澄清与协作的数据探索中未必更稳。** |

---

## 6. Observability / AIOps / Code-to-SQL / Migration

数据库内核团队常有一种无力感：线上出了故障，监控大盘满屏飘红，几百条 SQL 同时变慢，DBA 既分不清谁是引发雪崩的始作俑者、谁只是被锁和 I/O 连累的受害者；好不容易抓到那条烂 SQL，转身去微服务代码库里一搜，却发现它早已被三层 ORM 框架动态拼接得面目全非。本组 3 篇论文精准切中了数据库与业务研发交界处的治理痛点。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 16 | **Learned Root-Cause SQL Prioritization and Diagnosis for Complex Database Performance Issues** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803087) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803087) | 美团联合华东师范大学提出的复杂数据库性能异常根因 SQL 排序与诊断系统。线上雪崩时，单纯按执行耗时或次数排 Top SQL 往往会抓错人——一条耗时并不算最长的未走索引更新，可能正死死攥着热点行锁或打满 IOPS，拖慢了后面上千条无辜点查。该工作聚焦从全实例负载退化、锁等待与多维资源竞争信号中剥离因果关系，直接输出可行动的根因 SQL 诊断优先级。 |
| 20 | **SQLens: Continuous Code–to–SQL Visibility in the Wild** | [作者版 PDF](https://desert0616.github.io/pdf/SQLens.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803091) | 阿里巴巴打通业务代码与线上 SQL 双向映射的 **SQLens**。现代微服务里的 SQL 散落在 ORM 注解、动态条件分支与配置文件里，导致代码发版评审时看不见生成的慢 SQL，线上报警抓到慢 SQL 后又找不到该回滚哪个 Git 函数。SQLens 没有盲目把整座代码仓塞给大模型硬猜，而是**以静态程序分析锚定数据库调用点，驱动多协作智能体沿控制流与数据流精准还原 SQL 拼接模板与参数绑定，最后拿线上真实历史 SQL 日志做反向交叉验证**，沉淀出带版本演进的双向归因知识层。 |
| 25 | **From JSON to Duality: Automated Application Migration from Document to Relational Databases** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803096) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803096) | Oracle 围绕 JSON Relational Duality Views（关系—文档对偶视图）展开的应用自动化迁移实践。文档数据库胜在应用层读写嵌套对象的直观敏捷，却常在跨文档规范化一致性与复杂分析关联上吃亏；Oracle 的对偶视图允许底层按规范化关系表存储、上层继续按 JSON 文档读写。阅读全文时值得重点核查：它如何从存量文档负载与查询模式自动推导底层关系模式分解？嵌套更新冲突、约束语义与残余人工校验边界落在何处？ |

---

## 7. Time Series / Compression / Caching / Cloud Storage Resource Pooling

最后一组聚焦于云基础设施最底层的**利润率工程**：在超大规模集群里，网关代理为了扛突发峰值往往常年空转冗余，监控时序浮点数每秒都在吞噬PB级磁盘，而湖仓一体分析引擎则夹在“本地 DRAM 太贵装不下”与“对象存储直接拉取太慢”之间。想把这笔冗余省下来，前提是**资源池化与缓存协议必须认清不同数据的生命周期与读写语义差异**。

| **#** | **Paper** | **下载链接** | **主要内容与核心看点** |
|---:|---|---|---|
| 10 | **CLAPS: A Load-Aware Proxy Resource Pooling System for Reducing Resource Redundancy in Large-Scale Cloud Storage** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803082) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803082) | 字节跳动联合浙江大学研发的大规模云存储接入代理（Proxy）负载感知资源池化系统 **CLAPS**。在云存储架构中，无状态或轻状态的 Proxy 层过去常按业务线独立部署并按各自最高水位预留机器，造成惊人的常态算力闲置；可一旦粗暴混部共享，突发大流量租户又会瞬间击穿邻居的 P99 尾延迟。CLAPS 给出了兼顾动态削峰填谷、多租户隔离与故障爆炸半径控制的池化调度机制，其思路对数据库网关与 Serverless 连接池同样极具参考价值。 |
| 26 | **Analyzing and Improving Floating-Point Compression for Application Monitoring Time Series** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803097) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803097) | 可观测性厂商 Dynatrace 基于海量真实 APM（应用性能监控）监控流对浮点数无损压缩的实证剖析与改进。APM 指标流既有平稳贴近常数的利用率曲线，也有剧烈跳变的延迟毛刺和稀疏计数器，单一通用浮点压缩算法（如 Gorilla/Chimp 变体）很难通吃所有序列特征，且解压速度直接卡住高频聚合大盘的响应延迟。论文系统评测了真实分布下的算法短板，并结合轻量启发式特征选择改进了吞吐与压缩比平衡。 |
| 29 | **LakeMem: An Elastic Disaggregated-Memory Caching Layer for Analytical Processing Systems** | [作者版 PDF](https://cighao.com/papers/lakemem.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803100) | 阿里云联合厦门大学面向湖仓分析引擎打造的弹性解耦内存缓存层 **LakeMem**。这篇点破了一个长期被忽视的语义不对称：分析查询吃内存主要花在两块——**跨查询跨节点高度共享的只读底表列块**，以及**单节点单算子生命周期内高频读写的私有中间状态（Hash Table 溢出、Shuffle 缓冲）**。传统远端内存缓存把所有字节一视同仁，要么为私有状态多付了全局一致性协调税，要么丢掉了底表跨节点共享复用。LakeMem 据此拆出双路径架构：**PrivateCache** 走客户端主导模型低延迟承接私有溢出，**SharedCache** 走服务端全局协调模型最大化共享表命中率，并动态再平衡两侧配额。基于 DuckDB 在 DuckLake/smallpond 上的评测显示，内存受限查询较 DRAM-SSD 混合缓存提速 2.0–5.9×。 |

---

## 横向收束：从 29 篇工业论文看生产系统的三笔硬账

合上这 29 篇论文的目录，今年工业界在架构取舍上呈现出三条格外清晰的脉络：

1. **资源越解耦，“语义感知”越要往底层沉。** 早年存算分离讲究分层透明——文件系统只管块，远端内存只管页，Shuffle 只管搬字节。但到了 CloudJump III、LakeMem 和 FuxiShuffle 这里，大家异口同声地承认“语义盲目的通用层太贵了”：分层存储必须认得数据库页类型与快照边界，远端内存必须分清只读底表和私有溢出态，Shuffle 服务必须结合算子阻塞属性决定何时拉起下游。**解耦移除了硬件孤岛，而跨层传递轻量语义提示（Hints）则是把协调税重新压下去的唯一办法。**
2. **AI 进入数据栈正在分化为“严守底线的控制面”与“算清账单的数据面”。** 在优化器、可观测与运维控制面上（LOAM、ByteHouse、SQLens、美团根因诊断），工业界没有谁敢让黑盒模型裸奔接管线上决策——LOAM 靠领域自适应免除在线高危试跑，SQLens 靠静态分析框定范围再用真实日志验真；而在数据执行面上（Cortex AISQL），核心命题则是用关系代数改写和大小模型级联，把原本天价的大模型逐行推理压回企业付得起的成本区间。
3. **新能力的终极归宿往往是长进既有标准接口里，而不是再造一个孤岛系统。** 无论是 Aurora Limitless 把分布式分片藏在标准 PostgreSQL 连接背后、Poseidon 在一套引擎内弥合属性图与 RDF 并支持事务、LindormVector 与 ByteHouse 把向量过滤变成多模/数仓内生算子，还是 Enzyme 让普通 SQL 物化视图自动获得代价驱动的增量刷新——用户永远偏爱更少的数据搬迁、更稳定的接口契约（正如运行二十年的 Bigtable 所展示的那样），把异构复杂性留在内核内部消化，正是工业级数据库系统最深的护城河。

---

## 检索与阅读说明

本文整理自 [SIGMOD 2026 Industry Track](https://2026.sigmod.org/) 全部 29 篇录用论文目录（表内 `#1`–`#29` 保留原始整理序号以便交叉检索）。其中附有作者版或 arXiv 预印本的条目（如 CloudJump III、Aurora Limitless、LOAM、ByteHouse、Cortex AISQL、Enzyme、FuxiShuffle、Poseidon、Fabric-X、RIOT、Adobe Enterprise AI、ConDABench、SQLens、LakeMem 等）结合公开全文与实验章节提炼机制；仅公开 ACM DOI/摘要页面的条目则依据官方元数据与生产问题域梳理核心看点与精读追问方向。文中引述的性能加速比与成本节省比例均出自对应论文在特定工作负载与基线下的报告值，工程选型落地前建议对照原文复核负载特征与失效边界。

---
title: "SIGMOD 2026 Industry Papers"
date: 2026-08-09T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
categories:
  - 数据库
tags:
  - SIGMOD
  - 数据库
  - 论文
description: "SIGMOD 2026 工业界论文阅读笔记，29 篇论文按 7 大主题整理，涵盖云原生数据库、分布式 OLTP、查询优化、向量检索与 AI 数据基础设施等方向。"
draft: false
---

[SIGMOD 2026](https://2026.sigmod.org/) 今年 May 31 - June 05, 2026 在 Bengaluru, India 举行。每年我都会抽时间读工业界论文，关注系统在生产约束下怎样取舍。今年 Industry Track 共 29 篇论文，参与方包括 AWS、Microsoft、Google、Snowflake、Databricks、Oracle、IBM、Adobe、Dynatrace、Neo4j，以及阿里巴巴、字节跳动、腾讯、美团等国内公司。下面按方向整理为 7 个主题，每篇附上下载链接和个人的快速分析。

## 概览

按系统责任划分论文比按公司排列更便于比较：先看存储与事务约束，再看查询执行和 AI 数据路径，最后检查诊断与资源管理。以下七组是本文的阅读分类，不是会议官方分类。

| **主题** | **篇数** | **代表论文** |
|---|---|---|
| 1. Production Database / Cloud-Native DB / Storage | 7 | CloudJump III、Aurora Limitless、TDSQL-Boundless、Twenty Years of Bigtable |
| 2. Query Engine / OLAP / Data Warehouse / AISQL | 6 | Learned Query Optimizer (LOAM)、Cortex AISQL、ByteHouse、Enzyme |
| 3. Recommendation / Feature Engineering / Shuffle / ML Data Infrastructure | 3 | TokaDB、NebulaSQL、FuxiShuffle |
| 4. Graph / Consensus / Blockchain | 4 | Poseidon (Neptune Analytics)、RIOT、Fabric-X |
| 5. Vector / Multimodal / AI Data Platform | 3 | LindormVector、ConDABench |
| 6. Observability / AIOps / Code-to-SQL / Migration | 3 | SQLens、Root-Cause SQL Diagnosis |
| 7. Time Series / Compression / Caching / Cloud Storage Resource Pooling | 3 | LakeMem、CLAPS |

## 1. Production Database / Cloud-Native DB / Storage

生产数据库扩展需要同时保住事务语义与资源效率；拆分存储、计算或分片会引入新的路由、协调和恢复路径。阅读这一组时，应区分吞吐扩展与跨分片事务的代价。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 1 | **ByteGraph-Dione: An Adaptive Dual-Format Graph Engine with Hotspot Awareness and Transaction Efficiency for Production-Scale Workloads** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803073) / [ACM DOI](https://dl.acm.org/doi/10.1145/3788853.3803073) | ByteDance 的生产级图数据库/图引擎论文。关键词是 **dual-format graph engine、hotspot awareness、transaction efficiency**。**标题级阅读入口，机制待全文核对。** 可带着以下问题阅读：双格式各自服务哪些访问？热点识别与格式切换有何代价？遍历局部性与事务更新冲突如何协调？不能仅由标题断言它采用特定冷热切换算法。 |
| 9 | **Scalable Leader Leases For Multi Consensus Groups in CockroachDB** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803081) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803081) | CockroachDB 的多 Raft range / 多 consensus group 下 leader lease 扩展性问题。核心是如何让读请求在不走 quorum 的情况下安全服务，同时避免 lease 管理本身成为大规模 range 数量下的瓶颈。对分布式数据库工程师来说，它对应的是 **read lease、leaseholder、range movement、failover correctness、stale read safety** 这几个底层主题。 |
| 11 | **Scalable and Resilient Storage Tier for Azure SQL Hyperscale** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803083) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803083) | Azure SQL Hyperscale 的存储层设计。Hyperscale 这类系统的核心问题是把传统单机数据库的 page/log/storage path 拆成可扩展、可恢复、可独立扩容的云原生存储层。重点应包括 page server、log service、remote storage、redo/recovery、failover、replica catch-up、tail latency 和弹性扩展。 |
| 12 | **CloudJump III: Optimizing Cloud Databases for Tiered Storage** | [作者版 PDF](https://desert0616.github.io/pdf/CloudjumpIII.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803084) | Alibaba Cloud 的 CloudJump 第三代，重点是把 **page-level tiered storage** 集成进数据库内核，而不是放在 block/filesystem 层。论文明确说它在 buffer manager 的 eviction/flush 等控制点做 engine-aware placement，在 NVMe、远程块存储、对象存储（Object Storage）之间做数据分层，并与 recovery/snapshot 协议协同，目标是降低 fast tier footprint，同时维持接近本地 SSD 的吞吐和稳定 tail latency。这个方向非常值得数据库内核工程师看，因为它说明 cloud-native DB 的 tiering 不能只看 I/O trace，必须理解 page type、page age、table identity、buffer-pool residency、backup/recovery 语义。 |
| 18 | **Aurora PostgreSQL Limitless Database: Building a Highly Scalable OLTP Database** | [作者版 PDF](https://software.imdea.org/~gotsman/papers/limitless-sigmod26.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803089) | AWS Aurora PostgreSQL Limitless 的 scale-out OLTP 设计。论文摘要强调目标是让 PostgreSQL 兼容数据库在不需要复杂应用分片的情况下扩展到 millions TPS 和 petabytes data。主要内容应围绕 shard/routing、distributed transaction、global metadata、query pushdown、elastic scale-out、PostgreSQL compatibility，以及如何把 Aurora 原有 shared-storage 架构扩展到多 writer/multi-shard OLTP。 |
| 19 | **TDSQL-Boundless: A Distributed Database System for Large-scale Heterogeneous Multi-Table Workloads** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803090) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803090) | Tencent TDSQL 的分布式数据库系统论文。关键词是 **large-scale heterogeneous multi-table workloads**，说明它不只是单表分片 OLTP，而是面向多表、多业务形态、不同访问模式混合的生产负载。**具体机制待全文核对。** 后续检查自动分区与路由、跨分片事务、多表 Join、隔离和运维分别由哪些模块承担；这些是阅读问题，不是已经确认的功能清单。 |
| 24 | **Twenty Years of Bigtable** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803095) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803095) | Google Bigtable 20 年回顾。原始 Bigtable 是大规模结构化数据存储系统，服务 web indexing、Google Earth、Google Finance 等异构需求；**具体演进细节待全文核对。** 阅读时可沿 tablet、LSM/SSTable、metadata、compaction、replication 与租户隔离追踪变化，不预先把这些主题都归为论文已证明的结论。对系统工程师最有价值的是“长期生产系统如何演化”：接口稳定、内部不断重构。 |

## 2. Query Engine / OLAP / Data Warehouse / AISQL

引擎层的新策略只有在规划、执行和维护成本计入后才构成收益；模型选计划、AI SQL 和增量刷新分别改变不同环节。下面的数字需连同各论文的负载条件理解。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 2 | **Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions** | [arXiv PDF](https://arxiv.org/pdf/2602.07336) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803074) | 这篇非常适合 Query Optimizer 工程师精读。论文提出 LOAM，指出 learned optimizer 在真实分布式多租户数仓中面临四类生产挑战：动态执行环境导致 cost variance、缺失输入统计信息、传统模型 refinement 不现实、不同 workload 收益不确定。LOAM 通过 statistics-free plan encoding、历史执行信息、环境编码、domain adaptation 和 project selector，让 learned optimizer 更可部署；生产结果最高带来约 30% CPU cost savings。 |
| 3 | **Bitmap Filtering in the Fabric Data Warehouse** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803075) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803075) | Microsoft Fabric Data Warehouse 中的 bitmap filtering。Bitmap filter 本质是运行时过滤/半连接过滤的一种工程化形态，核心价值是在 join/build side 产生 compact bitmap，然后尽早过滤 probe/scan side，减少网络、I/O 和 join 输入规模。对 MPP/云数仓来说，难点不是算法本身，而是 optimizer 何时插入 filter、bitmap 如何分发、如何和列存 scan/vectorized execution/runtime filter 结合，以及如何避免 false positive/构建成本超过收益。 |
| 5 | **CoddSpeed: Hardware Accelerated Query Processing in Microsoft Fabric** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803077) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803077) | Microsoft Fabric 的硬件加速查询处理。重点应是把部分关系算子或数据路径映射到异构硬件/加速器上，并与 Fabric 的 query optimizer、execution engine、scheduler 和 fallback path 集成。真正难点在系统层：哪些算子值得 offload、host-device 数据移动是否抵消收益、如何做 cost model、如何保持 SQL 语义兼容、如何在生产环境做多租户隔离和资源调度。 |
| 8 | **ByteHouse: ByteDance’s Cloud-Native Data Warehouse for Real-Time Multimodal Data Analytics** | [arXiv PDF](https://arxiv.org/pdf/2602.08226) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803080) | ByteDance ByteHouse 的云原生实时多模态数仓。论文摘要提到统一表引擎、两层逻辑抽象、物理一致布局、SSD-backed cluster-scale cache CrossCache、虚拟文件系统 NexusFS、分析/批/增量执行模式、hybrid query 优化、tiered vector index 上的 runtime filtering，以及基于历史执行轨迹和 AI-assisted plan selection 的优化器。它代表 OLAP 向 **structured + vector + multimodal retrieval + incremental computation** 融合。 |
| 22 | **Cortex AISQL: A Production SQL Engine for Unstructured Data** | [arXiv PDF](https://arxiv.org/pdf/2511.07663) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803093) | Snowflake Cortex AISQL 生产系统论文，强烈建议精读。它把 AI semantic operations 作为 SQL 原生算子处理，问题是 AI inference cost/latency/selectivity 在优化阶段都很难估计。论文提出 AI-aware query optimization，把 LLM cost 作为优化目标；adaptive model cascades，用便宜 proxy model 处理大部分样本、把不确定样本交给强模型；semantic join rewriting，把某些 O(\|L\|×\|R\|) 的语义 join 改写成分类任务，论文报告 2–8×、2–6×、15–70× 等级的加速。 |
| 27 | **Enzyme: Incremental View Maintenance With Spark Declarative Pipelines** | [arXiv PDF](https://arxiv.org/pdf/2603.27775) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803098) | Databricks Enzyme，Spark Declarative Pipelines 背后的 IVM engine。核心是让 materialized views 成为数据 pipeline 的一等构件，并自动做 refresh planning，让用户关注业务逻辑，而不是手写增量维护策略。重点应包括 SQL operator coverage、增量 refresh plan、fallback 到 full refresh、pipeline DAG、状态管理、正确性和成本模型。对 Spark/MaxCompute/StarRocks 工程师来说，这对应“把 ETL/ELT 从 batch recompute 推向 optimizer-driven incremental maintenance”。 |

## 3. Recommendation / Feature Engineering / Shuffle / ML Data Infrastructure

特征处理与 Shuffle 优化依赖数据复用和状态生命周期；独立服务可能降低重复计算，也会增加网络依赖、资源调度与故障恢复成本。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 6 | **TokaDB: A Unified Storage Engine for Training-Serving Data Management in Large Recommendation Models** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803078) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803078) | ByteDance/HUST 的推荐系统存储引擎。题目里的 **training-serving data management** 很关键：大规模推荐模型既要离线训练读大规模历史样本/特征，又要在线 serving 读低延迟特征/embedding，还要处理 freshness、一致性、版本、冷热分层、写入放大等问题。标题提出统一训练与 serving 的数据管理目标；是否减少同步成本、如何处理一致性与资源争用，需要全文与实验支持，本文不据标题推定已达到这些收益。 |
| 7 | **NebulaSQL: A Large-scale Feature Computation System for Online Recommendation** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803079) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803079) | Alibaba 的在线推荐特征计算系统。它把特征工程抽象到 SQL/SQL-like 体系中，面向在线推荐的低延迟、高吞吐、实时/准实时更新。工程重点应包括特征表达语言、UDF、离线在线一致性、增量更新、状态管理、custom optimization rules 和推荐场景下的 SLA 保障。 |
| 14 | **FuxiShuffle: An Adaptive and Resilient Shuffle Service for Distributed Data Processing on Alibaba Cloud** | [arXiv PDF](https://arxiv.org/pdf/2602.22580) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803085) | MaxCompute/Alibaba Cloud 的生产 shuffle service。论文指出 shuffle 常因小随机 I/O、网络竞争、动态 job 特征和失败恢复成为瓶颈。FuxiShuffle 的核心设计包括：基于运行时信息动态选择 shuffle mode，progress-aware scheduling 下游 worker，为每个 shuffle chunk 自动选择 backup 策略，多副本 failover，精细内存管理，incremental recovery 避免失败后丢失大量进度。它本质上是在做 **data exchange operator 的服务化、弹性化和容错化**。 |

## 4. Graph / Consensus / Blockchain

图执行和共识协议不能用同一个吞吐指标比较：前者受访问模式影响，后者还受故障模型与一致性条件约束。本组共置便于索引，不表示它们解决同一问题。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 4 | **Poseidon: A OneGraph Engine** | [作者版 PDF](https://olafhartig.de/files/BebeeEtAl_SIGMOD2026_CameraReadyVersion.pdf) / [arXiv PDF](https://arxiv.org/pdf/2510.11166) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803076) | AWS Neptune Analytics 背后的 Poseidon graph engine。论文摘要说它支持 openCypher，并扩展语法以支持 RDF/LPG OneGraph 互操作；覆盖图 pattern matching、variable-length paths、aggregation、graph algorithm invocation、dynamic graph transaction。系统层亮点包括 lock-free adjacency list maintenance、secondary succinct indices、partitioned heaps、cost-based optimizer statistics、logical log durability。它是图数据库从“存储+遍历”走向“一体化 graph query + graph analytics + transactions”的代表。 |
| 15 | **G2+D: A High Performance Distributed Graph Mining System** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803086) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803086) | ByteDance 的分布式图挖掘系统。图挖掘和普通图遍历不同，常见任务包括 pattern mining、motif/subgraph enumeration、community/fraud/recommendation 相关分析，难点是中间状态爆炸、负载倾斜、通信量大和高阶 pattern 组合复杂。**具体实现待全文核对。** 分区、负载均衡、剪枝和压缩是值得检查的候选机制，不是本文已确认的实现。 |
| 21 | **Fabric-X: Scaling Hyperledger Fabric for Asset Exchange** | [IACR ePrint](https://eprint.iacr.org/2023/1717) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803092) | IBM Research 的 Hyperledger Fabric 扩展系统，面向 regulated asset exchange。ePrint 版本标题强调重新设计 Hyperledger Fabric architecture，以支持高吞吐的受监管资产交换应用。主要方向应包括交易 pipeline、ordering、validation、endorsement、state access、隐私/监管约束和吞吐瓶颈消除。对数据库视角来说，它和 deterministic transaction processing、serializability、replicated log、asset state machine 很接近。 |
| 23 | **RIOT: Replicated Independently-Ordered Transactions** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803094?download=true) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803094) | Neo4j/Nvidia/UC Berkeley 的 replicated transaction/consensus 论文。作者主页摘要说传统 Raft/Paxos 通过单 leader 和 totally ordered log 做状态机复制，简单但引入顺序瓶颈；RIOT 用 DAG entries 上的 decentralized coordination 取代中心化 leader/log replication。它的关键问题是：如果事务之间不冲突，能否独立排序、并行复制、并行提交，同时保持等价的可串行化/一致性语义。 |

## 5. Vector / Multimodal / AI Data Platform

向量检索收益必须连同召回率、过滤语义和更新成本评价；仅比较无过滤静态数据上的吞吐，无法说明混合查询是否可用。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 17 | **LindormVector: A Distributed Vector Engine on a Cloud-Native Multi-Model NoSQL Database** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803088) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803088) | Alibaba Cloud Lindorm 上的分布式向量引擎。官方产品文档显示 Lindorm vector engine 支持大规模向量存储、索引和检索，并支持多种索引算法、距离函数，以及 full-text/vector integrated retrieval，用于 RAG 场景。论文重点应在如何把 vector search 纳入 multi-model NoSQL：分片、索引构建、增量更新、过滤条件、全文+向量混合检索、成本和资源隔离。 |
| 13 | **Building, Serving, and Growing a Conversational AI Assistant for Enterprise** | [作者版 PDF](https://www.sumitbhatia.net/papers/sigmod26.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3801880) | Adobe 的企业级 Conversational AI Assistant 生产经验论文。重点不是单纯模型能力，而是企业场景中的构建、服务和增长：知识接入、权限、安全、评估、incident prevention、用户反馈、在线 serving、质量监控和组织内推广。它和数据库/数据平台的关系在于：企业 AI assistant 最终必须接企业数据、权限、审计和可观测性。 |
| 28 | **ConDABench: Interactive Evaluation of Language Models for Data Analysis** | [arXiv PDF](https://arxiv.org/pdf/2510.13835) / [Microsoft PDF](https://www.microsoft.com/en-us/research/wp-content/uploads/2026/05/3788853.3803099-1.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803099) | Microsoft 的 conversational data analysis benchmark。论文指出真实数据分析任务目标常常不完整、数据也不干净，需要多轮交互来澄清意图；现有 benchmark 很难评估这种交互式能力。ConDABench 通过 multi-agent workflow 生成 1,420 个 conversational data analysis problems，并提供 evaluation harness。结论很有意思：新模型能解决更多实例，但不一定更擅长需要长期、多轮、持续协作的任务。 |

## 6. Observability / AIOps / Code-to-SQL / Migration

诊断系统需要可核对的归因证据，而不只是合理解释；代码映射、执行画像和对照实验决定建议能否进入工程流程。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 16 | **Learned Root-Cause SQL Prioritization and Diagnosis for Complex Database Performance Issues** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803087) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803087) | Meituan/ECNU 的数据库性能诊断论文。题目说明它关注复杂性能问题下的 root-cause SQL prioritization：在海量 SQL、指标、慢查询、实例负载、资源竞争中，识别真正导致性能异常的 SQL，并给出诊断优先级。这个方向对云数据库/AIOps 很关键，因为生产问题通常不是“找一个慢 SQL”，而是从 workload-level degradation 中定位 causality 和 priority。 |
| 20 | **SQLens: Continuous Code–to–SQL Visibility in the Wild** | [作者版 PDF](https://desert0616.github.io/pdf/SQLens.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803091) | Alibaba 的 code-to-SQL visibility 系统。论文摘要非常明确：现代服务中的 SQL 被语言、框架、ORM、配置和抽象层隐藏，导致代码变更和 SQL 行为之间的联系丢失，带来性能回退、安全风险和诊断成本。SQLens 结合静态程序分析和 LLM-guided reasoning，从数据库 emission sites 出发，让 cooperative agents 沿 control/data flow 重建 SQL construction 和 parameter binding，再用历史 SQL logs 反向验证，形成 versioned knowledge layer，支持 code-to-SQL lookup 和 SQL-to-code attribution。 |
| 25 | **From JSON to Duality: Automated Application Migration from Document to Relational Databases** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803096) / [ACM DOI](https://dl.acm.org/doi/10.1145/3788853.3803096) | Oracle 的 JSON/document 到 relational/duality migration 论文。**迁移机制待全文核对。** 阅读重点是 JSON 访问如何映射到关系模式、哪些约束与事务语义可保留，以及自动生成映射后仍需多少人工修复；不能由题目推出完整的自动迁移能力。 |

## 7. Time Series / Compression / Caching / Cloud Storage Resource Pooling

资源池化收益取决于复用能否覆盖远程访问与协调成本；共享缓存还必须区分只读基础数据与私有中间状态，否则命中率不能反映正确性和隔离要求。

| **#** | **Paper** | **下载链接** | **主要内容分析** |
|---|---|---|---|
| 10 | **CLAPS: A Load-Aware Proxy Resource Pooling System for Reducing Resource Redundancy in Large-Scale Cloud Storage** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803082) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803082) | ByteDance/浙江大学的大规模云存储 proxy resource pooling。云存储系统里 proxy 层通常为了峰值和隔离而过度 provision，造成资源冗余。CLAPS 的方向是 load-aware pooling：在多租户/多业务负载下动态复用 proxy 资源，同时保证隔离、tail latency 和故障域控制。这个问题和数据库连接池、shuffle service、gateway/cache tier 的资源池化非常类似。 |
| 26 | **Analyzing and Improving Floating-Point Compression for Application Monitoring Time Series** | [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803097) / [ACM DOI](https://dl.acm.org/doi/abs/10.1145/3788853.3803097) | Dynatrace 的应用监控时间序列浮点压缩。监控数据有高写入、长保留、查询聚合频繁、浮点值变化模式复杂等特点。论文方向是分析现有 floating-point compression 在真实 APM 时间序列上的表现，并改进压缩策略；公开摘要关键词显示其关注 lossless compression、floating-point compression、lightweight compression、heuristic algorithm selection。 |
| 29 | **LakeMem: An Elastic Disaggregated-Memory Caching Layer for Analytical Processing Systems** | [作者版 PDF](https://cighao.com/papers/lakemem.pdf) / [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3788853.3803100) | Alibaba Cloud/Xiamen University 的 lakehouse 分析缓存系统。论文指出 shared-storage + open table format 上的分析负载经常是 memory-bound 的：引擎既要缓存大 base table 保吞吐，又要维护大量中间状态（hash table、shuffle buffer），disaggregated memory（DM）因此成为自然的弹性容量选择。但现有 DM cache 大多是 byte-uniform 的，忽略了 shared base-table 数据与 node-private 中间数据之间的语义不对称。LakeMem 采用 dual-path 架构：PrivateCache 用 client-centric 模型低延迟服务私有中间数据，SharedCache 用 server-coordinated、globally coherent 的方式服务共享 base-table 数据以最大化复用，并配合动态 rebalancing 机制在两条路径之间自适应分配内存。原型基于 DuckDB 实现，在 DuckLake/smallpond 上评测，memory-bound 查询相比 DRAM-SSD hybrid cache 提速 2.0–5.9×。 |

## 阅读取舍与后续核对

这 29 篇材料适合形成研究问题，不能据此推断整个行业的采用率。后续阅读可以围绕三组依赖展开：

1. **AI 改变哪一层决策，谁验证结果。** LOAM、ByteHouse、Meituan 和 SQLens 分别涉及计划选择、诊断与归因；Cortex AISQL 把推理纳入 SQL 执行，TokaDB、NebulaSQL 则组织训练与特征数据。前者要检查回归控制，后者要确认模型级联的质量约束，以及推理成本是否计入端到端收益。
2. **资源拆分以后，协调成本落在哪里。** Hyperscale、CloudJump III、LakeMem、CLAPS、FuxiShuffle 分别改变存储、页、内存缓存、接入层和中间数据的归属。Aurora Limitless、TDSQL-Boundless 与 CockroachDB 还需同时处理路由和事务。存算分离（Storage–Compute Disaggregation）或节点增加本身，都不能证明跨分片和故障场景中的扩展性。
3. **新增能力怎样进入已有接口。** LindormVector、ByteHouse 和 Poseidon 展示了向量、多模态或图模型的集成；Enzyme 将物化视图（Materialized View）维护交给优化决策，需继续核对增量适用范围、全量回退与新鲜度；Twenty Years of Bigtable 则提供了接口兼容与内部演进的案例。这里的集成趋势不构成对独立产品必要性或市场份额的判断。

本文仍是快速阅读笔记：表中依据标题提出的解释属于待核对假设，论文报告的倍数属于作者实验，并非本文复现。采用某项方案前，应回到全文确认数据规模、对照基线、故障模型和不适用条件；证据不足的条目只作为阅读入口。

---
title: "【Paper】数据库论文原文精读：67 篇阅读索引"
date: "2026-10-22T12:00:00+08:00"
slug: "database-papers-source-reading"
categories: ["数据库"]
tags: ["论文精读", "数据库系统"]
description: "从关系模型、优化器与执行引擎，到存储、事务与分布式共识的原文精读索引。"
draft: true
toc: true
paper_reading: true
---

数据库论文需要放回各自的问题和假设中阅读：优化器讨论搜索空间和估计，执行引擎讨论 CPU 与内存访问，存储和分布式协议则需要分别明确持久化、并发与故障语义。这个索引按机制组织，不用加速比给不同工作排名。

## 阅读路线

先读关系模型与系统架构，建立语义和模块边界；随后沿优化器、执行引擎和存储索引向下深入。事务、共识和一致性需要独立对齐故障模型，不能将单机结论直接迁移到分布式系统。

各篇先说明研究问题、机制和证据边界，再保留原文、翻译、误读与假设示例。书目信息和分析沿用本次已完成的报告，不代表重新复现了实验。全部文章当前为草稿；图片为原 PDF 选页截图，不包含整份论文。

## 优化器框架

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 1 | [The Cascades Framework for Query Optimization]({{< relref "2026-10-22-paper-the-cascades-framework-for-query-optimization/index.md" >}}) | 10 | 10 |
| 2 | [Shared-Nothing 架构上查询优化的并行化]({{< relref "2026-10-22-paper-parallelizing-query-optimization-on-shared-nothing-architectures/index.md" >}}) | 12 | 10 |
| 3 | [Volcano Optimizer Generator ：可扩展性与高效搜索的架构权衡]({{< relref "2026-10-22-paper-the-volcano-optimizer-generator-extensibility-and-efficient-search/index.md" >}}) | 10 | 18 |

## 连接重排

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 4 | [扩展关系代数查询处理框架以支持外连接：Rosenthal与Reiner (1984) 论文]({{< relref "2026-10-22-paper-extending-the-algebraic-framework-of-query-processing-to-handle-outerjoins/index.md" >}}) | 10 | 10 |
| 5 | [USING EELs, A PRACTICAL APPROACH TO OUTERJOIN AND ANTIJOIN REORDERING]({{< relref "2026-10-22-paper-using-eels-a-practical-approach-to-outerjoin-and-antijoin-reordering/index.md" >}}) | 18 | 10 |

## 代价模型

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 6 | [Seeking the Truth About Ad Hoc Join Costs]({{< relref "2026-10-22-paper-seeking-the-truth-about-ad-hoc-join-costs/index.md" >}}) | 27 | 10 |

## 统计与基数估计

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 7 | [Adaptive Query Processing in the Looking Glass]({{< relref "2026-10-22-paper-adaptive-query-processing-in-the-looking-glass/index.md" >}}) | 12 | 10 |
| 8 | [DB2 UDB 中自动化统计信息收集（ASC）机制]({{< relref "2026-10-22-paper-automated-statistics-collection-in-db2-udb/index.md" >}}) | 12 | 10 |
| 9 | [海量数据摘要的机制、误差契约与工程边界：Samples、Histograms、Wavelets、Sketches 深入研究]({{< relref "2026-10-22-paper-synopses-for-massive-data-samples-histograms-wavelets-sketches/index.md" >}}) | 296 | 16 |
| 10 | [直方图在数据库查询优化中的历史演进与内核机制]({{< relref "2026-10-22-paper-the-history-of-histograms/index.md" >}}) | 12 | 10 |
| 11 | [Universality of Serial Histograms]({{< relref "2026-10-22-paper-universality-of-serial-histograms/index.md" >}}) | 12 | 10 |
| 48 | [Naru：联合分布学习与逐步重要性采样的基数估计精读]({{< relref "2026-10-22-paper-deep-unsupervised-cardinality-estimation/index.md" >}}) | 14 | 30 |
| 50 | [LEO：DB2 2001 原型的执行反馈、统计关联与计划改进边界]({{< relref "2026-10-22-paper-leo-db2s-learning-optimizer/index.md" >}}) | 10 | 10 |
| 51 | [NeuroCard：全外联接采样、列分解与表子集扇出校正精读]({{< relref "2026-10-22-paper-neurocard-one-cardinality-estimator-for-all-tables/index.md" >}}) | 13 | 32 |

## 属性与函数依赖

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 12 | [Exploiting Functional Dependence in Query Optimization]({{< relref "2026-10-22-paper-thesis-exploiting-functional-dependence-in-query-optimization/index.md" >}}) | 334 | 10 |
| 13 | [An Efficient Framework for Order Optimization]({{< relref "2026-10-22-paper-an-efficient-framework-for-order-optimization/index.md" >}}) | 24 | 10 |
| 14 | [Incorporating Partitioning and Parallel Plans into the SCOPE Optimizer]({{< relref "2026-10-22-paper-incorporating-partitioning-and-parallel-plans-into-the-scope-optimizer/index.md" >}}) | 12 | 10 |

## 嵌套查询与去相关

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 15 | [Of Nests and Trees: 嵌套子查询、聚合与量词的统一处理框架]({{< relref "2026-10-22-paper-a-unitied-approach-to-processing-queries-that-contain-nested-subqueries-aggregates-and/index.md" >}}) | 12 | 10 |
| 16 | [分布式关系数据库R\*中嵌套查询的优化机制]({{< relref "2026-10-22-paper-optimization-of-nested-queries-in-a-distributed-relational-database/index.md" >}}) | 13 | 10 |
| 17 | [SQL-like与QUEL-like关联聚合嵌套查询的去相关化正确性研究]({{< relref "2026-10-22-paper-sql-like-and-quel-like-correlation-queries-with-aggregates-revisited/index.md" >}}) | 17 | 10 |
| 18 | [The Complete Story of Joins (in HyPer)]({{< relref "2026-10-22-paper-the-complete-story-of-joins/index.md" >}}) | 20 | 10 |
| 19 | [Unnesting Arbitrary Queries]({{< relref "2026-10-22-paper-unnesting-arbitrary-queries/index.md" >}}) | 20 | 10 |

## 查询执行引擎

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 20 | [编译型查询的自适应执行：从字节码解释到机器码的动态切换]({{< relref "2026-10-22-paper-adaptive-execution-of-compiled-queries/index.md" >}}) | 12 | 10 |
| 21 | [编译与向量化查询执行范式的深度对比]({{< relref "2026-10-22-paper-everything-you-always-wanted-to-know-about-compiled-and-cectorized-queries-but-were-af/index.md" >}}) | 14 | 10 |
| 22 | [Looking Ahead Makes Query Plans Robust]({{< relref "2026-10-22-paper-looking-ahead-makes-query-plans-robust/index.md" >}}) | 12 | 10 |
| 23 | [主存哈希连接在现代处理器架构上的硬件感知与硬件无关之争]({{< relref "2026-10-22-paper-main-memory-hash-joins-on-modern-processor-architectures/index.md" >}}) | 14 | 10 |
| 24 | [MonetDB/X100: Hyper-Pipelining Query Execution]({{< relref "2026-10-22-paper-monetdn-x100-hyper-pipelining-query-execution/index.md" >}}) | 14 | 15 |
| 25 | [Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age]({{< relref "2026-10-22-paper-morsel-driven-parallelism-a-numa-aware-query-evaluation-framework-for-the-many-core-ag/index.md" >}}) | 12 | 10 |
| 26 | [Query Evaluation Techniques for Large Databases：物理代数、外存匹配与并行执行的原文精读]({{< relref "2026-10-22-paper-query-evaluation-techniquesfor-large-databases/index.md" >}}) | 98 | 20 |
| 27 | [Relaxed Operator Fusion for In-Memory Databases: Making Compilation, Vectorization, and Prefetching Work Together At Last]({{< relref "2026-10-22-paper-relaxed-operator-fusion-for-in-memory-databases-making-compilation-vectorization-and-p/index.md" >}}) | 13 | 10 |
| 28 | [Volcano: 一个可扩展且并行的查询执行系统]({{< relref "2026-10-22-paper-volcano-an-extensible-and-parallel-query-evaluation-system/index.md" >}}) | 16 | 18 |

## 数据库架构

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 29 | [《Architecture of a Database System》：面向内核工程师的架构剖析]({{< relref "2026-10-22-paper-architecture-of-a-database-system-foundations-and-trends-in-databases/index.md" >}}) | 119 | 26 |

## 共识协议

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 30 | [A Generalised Solution to Distributed Consensus ：面向数据库内核工程师的共识协议抽象与Quorum弱化分析]({{< relref "2026-10-22-paper-a-generalised-solution-to-distributed-consensus/index.md" >}}) | 20 | 10 |
| 31 | [Consensus: Bridging Theory and Practice：Raft机制、证明边界与工程复核]({{< relref "2026-10-22-paper-consensus-bridging-theory-and-practice/index.md" >}}) | 258 | 10 |
| 32 | [Distributed Consensus Revised ：Paxos 算法族的泛化与弱化]({{< relref "2026-10-22-paper-distributed-consensus-revised/index.md" >}}) | 151 | 34 |
| 33 | [Raft 扩展版精读：复制日志、安全边界与实际评估]({{< relref "2026-10-22-paper-in-search-of-an-understandable-consensus-algorithm-extended-version/index.md" >}}) | 18 | 10 |

## 分布式一致性

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 34 | [Anna: A KVS For Any Scale：格合并、无协调 Actor 与一致性边界的原文精读]({{< relref "2026-10-22-paper-anna-a-kvs-for-any-scale/index.md" >}}) | 18 | 20 |
| 35 | [Ark：把选举、日志与写确认连接起来的共识协议精读]({{< relref "2026-10-22-paper-ark-a-real-world-consensus-implementation/index.md" >}}) | 12 | 16 |
| 36 | [PACELC原文精读：分区时的A/C与正常复制路径上的L/C]({{< relref "2026-10-22-paper-consistency-tradeoffs-in-modern-distributed-database-system-design/index.md" >}}) | 6 | 23 |
| 37 | [HLC精读：因果时间戳、一致切面与数据库保证的边界]({{< relref "2026-10-22-paper-logical-physical-clocks-and-consistent-snapshots-in-globally-distributed-databases/index.md" >}}) | 13 | 18 |

## 诊断与自动调优

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 38 | [Oracle 10g ADDM：统一时间记账、分层诊断与建议的证据边界]({{< relref "2026-10-22-paper-automatic-performance-diagnosis-and-tuning-in-oracle/index.md" >}}) | 11 | 10 |
| 39 | [Oracle 10g 自动 SQL 调优精读：SQL Profile、条件性建议与实测边界]({{< relref "2026-10-22-paper-automatic-sql-tuning-in-oracle-10g/index.md" >}}) | 12 | 22 |

## 关系模型与查询语言

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 40 | [C. J. Date的SQL批评原文精读：1983方言、正交性与NULL改写边界]({{< relref "2026-10-22-paper-a-critique-of-the-sql-database-language/index.md" >}}) | 47 | 27 |
| 41 | [Codd 1970关系模型原文精读：数据独立性、集合操作与一致性定义的边界]({{< relref "2026-10-22-paper-a-relational-model-of-data-for-large-shared-data-banks/index.md" >}}) | 11 | 21 |
| 42 | [RM/T原文精读：实体身份、语义约束与关系集合代数]({{< relref "2026-10-22-paper-extending-the-database-relational-model-to-capture-more-meaning/index.md" >}}) | 38 | 35 |
| 43 | [INGRES 1975原文精读：QUEL层次、查询分解与改写语义]({{< relref "2026-10-22-paper-ingres-a-relational-data-base-system/index.md" >}}) | 8 | 22 |
| 44 | [SEQUEL原文精读：块结构映射、重复语义与量化边界]({{< relref "2026-10-22-paper-sequel-a-structured-english-query-language/index.md" >}}) | 16 | 20 |

## 分析型数据格式

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 45 | [开放列式格式精读：压缩、转码与执行路径的可比性]({{< relref "2026-10-22-paper-a-deep-dive-into-common-open-formats-for-analytical-dbmss/index.md" >}}) | 13 | 18 |

## 网络与系统协同

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 46 | [The End of Slow Networks: It's Time for a Redesign]({{< relref "2026-10-22-paper-the-end-of-slow-networks-it-s-time-for-a-redesign/index.md" >}}) | 12 | 20 |

## 概率计数与抽样

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 47 | [Count-Min Sketch原文精读：非负碰撞噪声、加性界与分层应用]({{< relref "2026-10-22-paper-an-improved-data-stream-summary-the-count-min-sketch-and-its-applications-journal-of-a/index.md" >}}) | 18 | 30 |
| 49 | [Distinct Sampling：值级一致抽样、值内蓄水池与条件NDV报告精读]({{< relref "2026-10-22-paper-distinct-sampling-for-highly-accurate-answers-to-distinct-values-queries-and-event-rep/index.md" >}}) | 10 | 24 |
| 52 | [FM/PCSA原文精读：首零位图、随机平均与distinct估计边界]({{< relref "2026-10-22-paper-probabilistic-counting-algorithms-for-data-base-applications/index.md" >}}) | 28 | 34 |

## 数据库系统实践

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 53 | [Looking Back at Postgres：可扩展内核、失败路线与开源演化的历史精读]({{< relref "2026-10-22-paper-looking-back-at-postgres/index.md" >}}) | 9 | 30 |
| 54 | [Megastore：实体组事务、Paxos快速路径与Coordinator可见性屏障精读]({{< relref "2026-10-22-paper-megastore-providing-scalable-highly-available-storage-for-interactive-services/index.md" >}}) | 12 | 36 |

## 存储介质与缓存

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 55 | [五分钟法则三十年后：缓存经济阈值与存储层级的演示稿精读]({{< relref "2026-10-22-paper-the-five-minute-rule-thirty-years-later-and-its-impact-on-the-storage-hierarchy/index.md" >}}) | 29 | 27 |

## 存储结构与索引

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 56 | [Fractal Trees与LSM：缓冲写、合并策略及放大口径的白皮书精读]({{< relref "2026-10-22-paper-a-comparison-of-fractal-trees-to-log-structured-merge-lsm-trees/index.md" >}}) | 15 | 32 |
| 57 | [CaaS-LSM：压实服务化、混合调度与故障回退的源文研究]({{< relref "2026-10-22-paper-caas-lsm-compaction-as-a-service-for-lsm-based-key-value-stores-in-storage-disaggregat/index.md" >}}) | 28 | 10 |
| 58 | [RUM猜想：读、更新与空间放大的模型、边界及设计路线精读]({{< relref "2026-10-22-paper-designing-access-methods-the-rum-conjecture/index.md" >}}) | 6 | 25 |
| 59 | [Bourbon：不可变 SST 的学习索引、训练摊销与实测边界]({{< relref "2026-10-22-paper-from-wisckey-to-bourbon-a-learned-index-for-log-structured-merge-trees/index.md" >}}) | 18 | 10 |
| 60 | [LeanStore: In-Memory Data Management Beyond Main Memory]({{< relref "2026-10-22-paper-leanstore-in-memory-data-management-beyond-main-memory/index.md" >}}) | 12 | 20 |
| 61 | [LSM存储综述精读：归并策略、调优基线与多索引正确性]({{< relref "2026-10-22-paper-lsm-based-storage-techniques-a-survey/index.md" >}}) | 25 | 35 |
| 62 | [SuRF论文精读：有序近似过滤、FST布局与边界核验]({{< relref "2026-10-22-paper-surf-practical-range-query-filtering-with-fast-succinct-tries/index.md" >}}) | 14 | 34 |
| 63 | [原始LSM-tree论文精读：Rolling Merge、两重摊销与成本最优的边界]({{< relref "2026-10-22-paper-the-log-structured-merge-tree-lsm-tree/index.md" >}}) | 32 | 40 |
| 64 | [Umbra: A Disk-Based System with In-Memory Performance]({{< relref "2026-10-22-paper-umbra-a-disk-based-system-with-in-memory-performance/index.md" >}}) | 7 | 20 |

## 事务与并发控制

| 序号 | 论文精读 | PDF 页数 | 原文精读组数 |
|---:|---|---:|---:|
| 65 | [Generalized Isolation Level Definitions：版本历史、谓词依赖与PL隔离级别精读]({{< relref "2026-10-22-paper-generalized-isolation-level-definitions/index.md" >}}) | 12 | 24 |
| 66 | [Percolator：主锁提交、快照读取与可折叠通知的原文精读]({{< relref "2026-10-22-paper-large-scale-incremental-processing-using-distributed-transactions-and-notifications/index.md" >}}) | 14 | 36 |
| 67 | [Staring into the Abyss：千核模拟中的并发控制瓶颈与证据边界]({{< relref "2026-10-22-paper-staring-into-the-abyss-an-evaluation-of-concurrency-control-with-one-thousand-cores/index.md" >}}) | 13 | 34 |

## 来源与限制

共 67 篇、2210 个 PDF 物理页，保留 1252 组原文解读、3244 条证据和 131 张截图。

原文定位、OCR 转写和解释正确性是不同层次的检查。本文没有把 AI 编辑复核称为人工核验，尚未核实的出版信息保持未核实状态。正式发布前，仍需检查引用比例、图片使用权、公式和实验条件。

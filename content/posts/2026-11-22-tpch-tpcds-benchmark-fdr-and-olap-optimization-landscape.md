---
title: "从 2026 TPC-H / TPC-DS 公开榜单拆解 OLAP 性能优化全景：FDR 密码、开源横评与论文脉络"
slug: "tpch-tpcds-benchmark-fdr-and-olap-optimization-landscape"
date: 2026-11-22T18:40:00+08:00
lastmod: 2026-11-22T18:40:00+08:00
categories:
  - 数据库
  - 大数据
  - 系统架构
tags:
  - TPC-H
  - TPC-DS
  - OLAP
  - MaxCompute
  - Hologres
  - StarRocks
  - DuckDB
  - Umbra
  - 优化器
  - 向量化
description: "从 2026 年 TPC-DS 100TB（MaxCompute QA 2.0、Tencent DLC）与 TPC-H 3TB/30TB（Hologres 4.1 / V1.3）公开 FDR 关键数字与 DDL 物理布局切入，横向剖析七大开源分析引擎的优化路线，并沿逻辑改写、代价优化与信息旁路传递、编码布局、执行微架构、Out-of-Core Spill 及 GPU 异构计算展开近三十年核心论文与工程脉络。"
draft: false
toc: true
math: true
---

看一份 TPC-H 或 TPC-DS 公开榜单，外行只看最上面那行加粗的 QphH / QphDS 总分，而数据库内核工程师往往直接翻开几十页的 **Full Disclosure Report（FDR）**，盯住三组最枯燥的数字：**内存与原始数据的比值、单机网卡带宽与分片/分桶数、以及建表 DDL 里到底写了 `PARTITIONED BY` 还是 `CLUSTERED BY + SORTED BY`**。

因为真正决定百 TB 级分析查询快两个数量级的，从来不是营销口径，而是引擎如何用一套环环相扣的设计，去撞破四道冷酷的物理与理论墙：**I/O 与数据搬运墙、CPU Cache 与内存带宽墙、基数估计不确定性墙，以及并行长尾与内存硬上限墙**。

下面先从 **2026 年最新公开的 TPC-DS 100TB 与 TPC-H 3TB/30TB 标杆 FDR** 出发，把这批硬核数字背后的物理布局与算力密码拆清楚；随后横向对照 **StarRocks、DuckDB、HyPer/Umbra、Spark（Gluten/Comet）、ClickHouse、Trino/Presto 与 GPU 原生引擎**七大开源体系，把全栈优化手段放回近三十年数据库顶会论文的理论脉络中系统收束。

---

## 一、先读懂 2026 公开 TPC-H / TPC-DS 榜单与 FDR 密码

TPC-H 与 TPC-DS 虽然经常并列提起，但站在内核视角，它们考察的并不是同一套能力：

| 维度 | TPC-H（22 条查询 + RF1/RF2） | TPC-DS（99 条查询 + 维护测试） |
| --- | --- | --- |
| **数据模型** | 8 张表，以 `lineitem` / `orders` 为中心的经典星型/链式结构 | 24 张表（多事实表 + 共享维度表），复杂雪花与多事实联合模型 |
| **SQL 形态** | 大比例扫描、复杂算术/`CASE`、相关子查询（Q2/Q17/Q20/Q21）、3–6 表 Join、实时批量增删（RF1/RF2） | 8–15+ 表 Join（Q17/Q25/Q64）、重度多分支 CTE（Q1/Q4/Q11/Q23/Q47/Q74）、`ROLLUP`/`GROUPING SETS`（Q5/Q14/Q67/Q70/Q80）、复杂窗口与 TopN、显著数据倾斜与 NULL |
| **核心瓶颈** | **执行引擎微观效率与布局**：扫描裁剪、SIMD 向量化、低基数字符串、Hash Join/Agg 局部性、子查询消除 | **优化器与系统鲁棒性**：大连接搜索空间、基数误差放大、CTE 共享权衡、跨节点信息传递、倾斜与超内存 Spill 稳定性 |

TPC 审计严禁使用物化视图或针对单条 SQL 的预计算结果，但完全允许水平分区、分桶分布、物理排序聚簇、辅助索引与列存编码。把 2026 年现行及近年标杆公开 FDR 摆在同一张表里，每一项硬件与 DDL 选择背后的工程意图都会跃然纸上：

| 公开榜单条目 | 基准与规模 | 性能与性价比（公开值） | 集群算力、内存与网络 | 存储与 DDL 物理布局（FDR 披露） |
| --- | --- | --- | --- | --- |
| **[MaxCompute QA 2.0 v5.57](https://www.tpc.org/5018)**（[FDR](https://www.tpc.org/results/fdr/tpcds/alibaba_cloud~tpcds~100000~alibaba_cloud_maxcompute~fdr~2026-08-19~v01.pdf)，2026-08-19，SF100000 现行 #2） | TPC-DS 4.0.0<br>**100 TB**（SF100000） | **133,518,293 QphDS**<br>20.36 CNY/kQphDS<br>三年总价 2,717,787 CNY<br>（Load 1,771.5s / Power 1,341.6s / Throughput 2,604.5s & 3,133.8s） | 1,600 CU Serverless：64 × `ecs.g9ae.8xlarge` Pod（AMD EPYC 9T95），标称 32 vCPU / 128 GB（可分配 25 CU），合计 **2,048 vCPU / 8,192 GB 内存**，网络 **16 Gbps** | 每 Pod 3,337 GB ESSD + MaxCompute Storage 179,650.56 GB（总 393,218.56 GB，存储比 **3.94**）；AliORC 事务内表（Standard 档计一副本压缩量）；**无 `PARTITIONED BY`**，24 表全用 `CLUSTERED BY + SORTED BY`（9 大表 1,600 Bucket，事实表多按 `item_sk` 分桶、按 `date/time` 排序；15 维表 1 Bucket） |
| **Tencent Cloud DLC（SuperSQL 1.0）**（TPC-DS 4.0.0，SF100000 现行 #1） | TPC-DS 4.0.0<br>**100 TB**（SF100000） | **653,611,141 QphDS**<br>11.04 CNY/kQphDS<br>总价 7,215,288 CNY | Intel Xeon 6983P-C 集群（**4,544 核/线程**），TencentOS Server 4.4 | 云原生湖仓高密度算力集群，单位核效能与性价比刷新 SF100000 纪录 |
| **[Hologres 4.1](https://www.tpc.org/3414)**（[FDR](https://www.tpc.org/results/fdr/tpch/alibaba_cloud~tpch~3000~alibaba_cloud_hologres~fdr~2026-06-26~v01.pdf)，2026-06-29，SF3000 现行 #1） | TPC-H 3.0.1<br>**3 TB**（SF3000） | **8,443,627.5 QphH**<br>（Power 8,789,205.2 / Throughput 8,111,637.4，8 流）<br>390.47 CNY/kQphH<br>总价 3,296,977 CNY（Load 00:20:31） | 69 容器（2 Gateway + 1 JobMaster + 1 MetaStore + 1 HoloMaster + 64 HoloWorker×16C/64G），合计 **1,040 vCPU / 4,160 GB 内存**，全 **100 Gbps**（F91 服务器，公开详情未披露 CPU 型号） | 22 节点 × 7,920 GB NVMe + Pangu V2.0 6,200 GB（+50 GB refresh 平文件）；**320 data shards**（每 Worker 5 Shard），按 `distribution_key` hash 均匀自动映射，表级无副本，冗余由 Pangu 多副本与纠删码承担 |
| **[Hologres V1.3](https://www.tpc.org/3384)**（[FDR](https://www.tpc.org/results/fdr/tpch/alibaba~tpch~30000~hologres~fdr~2022_10_20~v01.pdf)，2022-10-31，已过期里程碑） | TPC-H 3.0.1<br>**30 TB**（SF30000） | **27,865,885.7 QphH**<br>1,591.87 CNY/kQphH | 459 容器、**11,000 vCPU / 88,000 GB 内存**，网络 **25 Gbps** | **3,456 shards**（Table Group 共置），AliORC 列存 + 88 TB 内存热缓存，Pangu 管底层冗余 |

![2026 TPC-H / TPC-DS 公开 FDR 物理密码与 OLAP 引擎全栈优化图谱](/images/diagrams/olap-tpc-fdr-and-optimization-stack.svg)

把这四份 FDR 拆开细读，有三条极其震撼的工程洞察藏在数字缝隙里：

1. **MaxCompute 100TB 的神来之笔：为什么彻底放弃 `PARTITIONED BY`，改用“9 大表 1,600 桶（按 `item_sk`）+ 桶内按 `date/time` 排序 + 15 维表 1 桶”？**
   - **算力与分桶严格 1:1 对齐**：64 Pod × 25 可分配 CU = 1,600 CU，9 张大表全部设 **1,600 Bucket**，恰好 1 Bucket 对齐 1 CU——既彻底杜绝传统“按天分区 × 分桶”制造几十万小文件与元数据爆炸，又保证单查询瞬间吃满 1,600 CU 无长尾；
   - **在 16 Gbps 窄网卡下靠分桶共置救命**：每台 Pod 只有 16 Gbps（2 GB/s）网络带宽，如果多张百亿级事实表（`store_sales`、`catalog_sales`、`web_sales`、`inventory`）在关联 `item_sk` 时全网 Shuffle，窄网卡会被瞬间打爆。全部按 `item_sk` Hash Clustering 后，跨事实表与 `item` 的重关联直接退化为本地共置（Colocated）执行，网络压力骤降；
   - **用桶内排序替代日期分区裁剪**：桶内 `SORTED BY date/time` 配合 AliORC Stripe 的 Min/Max ZoneMap，照样能在不建静态日期分区的前提下跳过绝大多数无关时间块；而 15 张维表全部压成 **1 个 Bucket**，扫描零碎片、Broadcast 零拼接；
   - **仅 8 TB 内存扛 100 TB 原始数据（内存比仅 8%！）**：相比纯内存硬扛方案，MaxCompute 只用 8,192 GB 总内存 + 每 Pod 3.3 TB 本地 ESSD 就在 22 分钟内跑完 99 条 100TB Power 查询，靠的就是高压缩比列存 + 极少跨网 Shuffle + 高效本地 NVMe 缓存与 Out-of-Core Spill。
2. **Hologres 4.1（2026）对比 V1.3（2022）：四年间的带宽、内存比与并发跃迁**
   - 2022 年 V1.3 跑 30TB 时用了 **88 TB 内存（2.93× 原始数据）** 和 25 Gbps 网卡；2026 年 4.1 跑 3TB 仅用 **4.16 TB 内存（1.39× 原始数据）** 与 **100 Gbps** 高速网络，64 个 HoloWorker 均匀承载 320 个 Shard（每节点 5 Shard）。
   - 最惊人的数字在于 **Power（8,789,205）与 8 并发流 Throughput（8,111,637）几乎 1:1 持平（比值仅 1.08）**——这说明在 8 路重并发与 Refresh 流混跑时，引擎的并发调度、锁粒度、100 Gbps 节点级 Shuffle 与 Pangu V2.0 存储吞吐几乎没有相互踩踏与长尾退化。
3. **Tencent DLC（6.53 亿 QphDS / 11.04 CNY）vs MaxCompute（1.33 亿 / 20.36 CNY）的规模经济学**
   - 在同一 100TB 赛道上，Tencent DLC 投入约 2.2× 的新一代高密核数（4,544 核 Intel Xeon 6983P-C，总价 721.5 万 CNY），跑出 **4.9× 的 QphDS 总分**，并把单位性价比（CNY/kQphDS）进一步压低近一半。这印证了在百 TB 级复杂雪花基准里，**更大内存驻留窗口 + 新一代高带宽 CPU 核密度**能够跨越关键工作集拐点，释放非线性的吞吐与成本红利。

---

## 二、全景横评：七大开源分析引擎的优化路线图

从公开云数仓 FDR 回到开源内核代码，不同开源引擎因定位差异（单机嵌入式、分布式 MPP、批处理大数据、湖仓联邦、GPU 原生），在四道物理墙上的工程取舍呈现出清晰的谱系：

| 引擎 / 体系 | 执行模型 | 优化器核心 | 字符串与编码王牌 | 运行时过滤 / SIP | Join & Agg 微架构亮点 | Spill / 内存治理 |
| --- | --- | --- | --- | --- | --- | --- |
| **StarRocks** | C++ 向量化 + Morsel Pipeline | Cascades CBO + 分级 Join Reorder（Exhaustive/DP/Greedy/Outer） | **表级 Global Low-Cardinality 字典**（`int32` 贯穿全链路） | Join GRF（AVX2 `SimdBlockFilter`/Bitset/多列）+ **TopN & Agg TopN RF** | Range Direct Mapping Join、`SmallFixedSize`/`phmap`/Two-Level Agg、COW Column | Spillable HashJoin / Partitionwise Agg / Sort |
| **DuckDB** | C++ 向量化 + Morsel Pipeline | 规则 + DPccp / 启发式 Join Reorder + **General Unnesting** | **German String**（4B len + 4B prefix + 8B inline/ptr）+ 轻量列压缩 | MinMax / Bloom Dynamic Filter + 动态 TopN Filter | **Perfect Hash Join / Agg**（密集整型直查）、Radix Partitioned Join | 统一 Buffer Pool + 运行时压缩列溢写（ICDE'24 / VLDB'25） |
| **HyPer / Umbra** | **Umbra IR + LLVM 编译执行**（Flying Start） | **DPhyp** + 超大连接自适应 + **General Unnesting** | **German String**（首创者） | Sideways Information Passing + **Diamond Hardened Joins**（Lookup & Expand） | 代码生成算子融合（零物化寄存器传递）+ LeanStore 可变长页 | LeanStore Pointer Swizzling + 弹性 out-of-core |
| **Spark + Gluten (Velox) / Comet (DataFusion)** | JVM Tungsten Codegen → **C++/Rust Native 向量化卸载** | Catalyst RBO/CBO + **AQE（运行时自适应）** | Velox `StringView`（German String 风格）+ `LazyVector` + `VectorEncoding` | **DPP（动态分区裁剪）** + Runtime Bloom Filter + AQE 动态转 Broadcast | AQE 动态拆 Skew Join / 合并分区；Velox `MemoryArbitrator` 统一仲裁 | Shuffle 落盘天然容错 + Velox 分区 Spill |
| **ClickHouse** | C++ 向量化 + 极致 SIMD 特化 | 早期偏 RBO，新版 Analyzer v2 逐步引入 CBO | `LowCardinality(String)` + `COW<IColumn>` 零拷贝 | Primary Key 稀疏索引 + Skipping Index（MinMax/Bloom）+ `PREWHERE` | 两级哈希表、按 Key 宽度高度特化的 Agg/Join 实现 | External Sort / GroupBy / Join（传统强项在单宽表） |
| **Trino / Presto (Prestissimo)** | Java 向量化/字节码 → **Velox C++ Native Worker** | Cascades/Iterative CBO（侧重联邦外表统计） | Velox `StringView` + 字典/RLE 延迟解码 | **Dynamic Filtering**（Broadcast/Partitioned 下推至 Iceberg/Hive/Parquet） | 联邦外表并发 Split 调度 + 算子级本地内存管理 | FTE Exchange Spooling + Velox 内存仲裁 |
| **RAPIDS / Sirius / Crystal (GPU)** | CUDA SIMT 并行（`libcudf` / Block-at-a-Time 融合） | 复用宿主前端（Spark / DuckDB）经 **Substrait** 下发 | 定长列直接驻留 HBM；变长字符串仍是短板 | GPU 极速并行 Filter / Partition + 显存内管道驻留 | TB/s HBM 带宽吞吐；无分支算子提速显著 | RMM + **HBM → Host DRAM → NVMe** 三级溢写与 CPU Fallback |

下面沿着**逻辑改写 → 代价优化与信息旁路传递 → 编码与物化 → 执行微架构与 Spill → GPU 异构前沿**五个层面，拆解每一项核心技术背后的论文原点与工程边界。

---

## 三、逻辑改写层：消除“根本不该发生的计算”

逻辑优化的核心是代数等价变换：在进入昂贵的物理扫描与 Shuffle 之前，直接砍掉重复扫表、中间行数膨胀与无效排序。

### 3.1 相关子查询去关联：General Unnesting 与 Subquery-to-Window

TPC-H 的 Q2、Q4、Q17、Q20、Q21、Q22 与 TPC-DS 的 Q1、Q6、Q23、Q30、Q95 充斥着相关子查询。如果朴素按 Nested Loop Apply 执行，复杂度会直接恶化为 \(O(|R| \cdot |S|)\)。业界由此沉淀出两条互补的黄金路线：

1. **通用去关联（General Unnesting）**——*Thomas Neumann & Alfons Kemper, "Unnesting Arbitrary Queries" (BTW 2015)*：
   传统数据库靠为特定 SQL 模板手写白名单规则，一旦遇到 `OR`、多层嵌套、`LIMIT`、非等值聚合或 `LATERAL` 便束手无策。Neumann 等人将任意相关子查询抽象为 **Dependent Join**（\(T_1 \bowtie_{\text{dep}} T_2\)），提取外层去重后的关联域 \(D = \pi^{\text{distinct}}_{\text{corr}}(T_1)\)，并证明 \(D\) 可以沿任意关系代数算子（Select、Project、Join、Outer Join、Agg、SetOp、Window）机械下推至叶节点变为普通等值 Join。HyPer、Umbra 与 **DuckDB** 均实现了这一框架。
2. **子查询转窗口函数（Subquery-to-Window）**——*Bellamkonda et al., "Enhanced Subquery Optimizations in Oracle" (VLDB 2009)*：
   以 TPC-H **Q17**（`l_quantity < 0.2 * (SELECT avg(l_quantity) FROM lineitem WHERE l_partkey = p_partkey)`）及 TPC-DS **Q1 / Q6** 为例，即便做了普通去关联，仍要扫两遍超大事实表并做一次大 Hash Join。识别出内外层同表且关联键一致后，直接改写为 `AVG(l_quantity) OVER (PARTITION BY l_partkey)`，**整整一遍大表 I/O 与一次超大 Join 直接归零**。

### 3.2 CTE 复用：物理代价决策而非盲目物化

TPC-DS 中超过三分之一的复杂查询（Q1/Q4/Q11/Q14/Q23/Q47/Q64/Q74/Q78/Q80）会多次引用同一 CTE。*Amr El-Helw et al. (VLDB 2015, Greenplum Orca)* 早已证明，“永远内联”与“永远物化”都是错的：
- **盲目内联**：4 表 Join 的 CTE 引用 4 次就重算 4 遍；
- **盲目物化**：若分支 A 过滤 `year = 2000`、分支 B 过滤 `year = 2001`，物化复用会把过滤屏障卡死，甚至在 MPP 中因下游要求的物理分布不同而引入额外 Exchange 与内存阻塞。

因此 Orca 与 StarRocks 将 CTE 拆为 `CTEAnchor / CTEProduce / CTEConsume` 放入 Cascades Memo 与内联方案同台比价，并配套列裁剪并集、谓词 `OR` 合并下推与最大 `Limit` 下推（详参[《从 ORCA CTE Reuse 到 StarRocks ROLLUP 下推》]({{< relref "2026-10-25-orca-cte-reuse-starrocks-rollup-pushdown.md" >}})），同时设编译保护阈值防止 Memo 空间爆炸；Spark AQE 则进一步在物理层通过 `ReuseExchange` 复用相同子树的 Shuffle 产物。

### 3.3 聚合下推、ROLLUP 与常见等价改写

- **Eager Aggregation（*Yan & Larson, VLDB 1995*）**：当可拆分聚合（`SUM`/`COUNT`/`MIN`/`MAX`）位于“亿级事实表 Join 维表”上方且 Join 键被分组键覆盖时，将部分聚合下推至 Join 下方能把事实表先压缩 1–2 个数量级。但若维表过滤选择率极高（滤掉 99% 行），反而先 Join 再聚合更快——因此现代优化器会先跑逻辑 Join Reorder 再结合缩减率决策。
- **`ROLLUP / GROUPING SETS` 专项（TPC-DS Q5/Q14/Q18/Q27/Q67/Q70/Q80）**：传统 `Repeat/Expand` 算子把行复制 \(K\) 倍后才 Shuffle，放大流量数倍；现代引擎会先按最细粒度预聚合再上卷，或按代价改写为 `CTE + UNION ALL`。
- **其余高频必杀改写**：等价类谓词派生（`a.k = b.k AND a.k > 10` 推导出 `b.k > 10` 穿透至双侧分区裁剪）；复杂 `OR` 提取公共下推谓词（攻克 TPC-H Q19）；`RANK() OVER (PARTITION BY ... ORDER BY ...) <= 100` 转为 `PartitionTopN` 有界堆截断（攻克 TPC-DS Q67/Q70/Q86）；以及将昂贵字符串/算术投影上提至 `LIMIT N` 之后求值。

---

## 四、代价优化与信息旁路传递（SIP）：穿越基数估计的迷雾

### 4.1 Join Reorder 演进与 JOB 基准的警示

TPC-DS 里 8–15+ 表的雪花连接把优化器逼进了双重困境：**搜索空间组合爆炸**与**基数估计误差放大**。

搜索算法历经了四代演进：从 **System-R（SIGMOD 1979）** 左深树 DP、**Cascades（Graefe 1995）** 自顶向下 Memo + 物理属性强制器，到 **DPccp / DPhyp（Moerkotte & Neumann, VLDB 2006 / SIGMOD 2008）** 仅枚举连通子图对并用超图原生支持 Outer/Semi/Anti Join 的完整 Bushy Tree，再到 **超大连接自适应算法（SIGMOD 2018）** 用 Linearized DP / IDP 在毫秒级处理 20+ 表查询。

然而，Viktor Leis 等人在里程碑论文 *"How Good Are Query Optimizers, Really?"*（**VLDB 2015, Join Order Benchmark**）中给出了残酷的实证：**主流优化器选出灾难性坏计划，90% 源于基数估计偏差（属性独立性假设失效、多表连乘误差指数级放大），而非代价公式权重不准。** 工业界因而转向两手抓：一手强化单列直方图、MCV、多列相关性统计与运行时采样（Umbra *Aggregate Estimates*, VLDB 2021）；另一手引入 **Spark AQE** 式运行时自适应，在 Shuffle 阶段边界读到真实物化字节数后，动态将 Sort-Merge Join 转为 Broadcast Hash Join、合并碎小分区或拆解倾斜大分区。

### 4.2 信息旁路传递（SIP）的学术革命：从 Yannakakis 到 Predicate Transfer 与 Diamond Joins

既然静态基数估计永远无法百分百准确，过去十年数据库优化领域最具颠覆性的跨引擎共识便是 **Sideways Information Passing（SIP）**——**与其把身家全押在静态 Join 顺序上，不如在运行时把小表过滤出的键集合尽可能早地回推到事实表扫描端！**

这条理论主线极其完整：

1. **理论原点（Yannakakis, VLDB 1981）**：对任意无环连接（涵盖绝大多数星型与雪花模型），先做两轮半连接缩减删掉所有悬挂元组再执行 Join，可保证中间结果大小最坏情况线性于输入与输出之和（实例最优）。
2. **缓存友好 SIMD Bloom Filter（Putze et al., ACM JEA 2009）**：提出 `Blocked Bloom Filter`，单次哈希全部落在同一条 256-bit / 512-bit Cache Line 里，一条 AVX2/NEON 指令并行探测 8 个 bit，彻底消灭多次随机访存的 L3 Cache Miss。
3. **LIP：Lookahead Information Passing（PVLDB 2017）**：证明在星型模型中，将所有维表构建的轻量 Bitvector/Bloom Filter 汇聚为自适应过滤链提前滤掉事实表，**几乎抹平了不同 Join 顺序之间的性能差距**。
4. **Data-Induced Predicates（PVLDB 2019）**：把跨 Join 诱导出的 MinMax / Bloom 谓词下推至分布式集群的分区与列存文件块级裁剪（直接对应 Databricks 的 **DFP** 与 Trino Dynamic Filtering）。
5. **Predicate Transfer & RPT+（CIDR 2024 / VLDB 2026）**：把 LIP 从单事实表星型模型推广到**任意多事实表雪花拓扑**，用紧凑 Bloom Filter 模拟 Yannakakis 的前向/后向两轮半连接缩减；**RPT+（VLDB 2026）** 进一步加入动态开销门控，防止在低过滤率查询上产生负收益。
6. **Diamond Hardened Joins（Umbra, PVLDB 2024）**：针对多对多菱形连接，将二元 Hash Join 拆成 **Lookup（只查计数/指针不立刻展开）** 与 **Expand（延迟展开）** 两个可重排物理子算子，在复杂菱形子图上消灭中间爆炸。

工业界中，StarRocks 把这一理念推进到 **Global Runtime Filter（含 AVX2 `SimdBlockFilter`、`BitsetFilter`、多列与穿越 Outer Join）+ TopN / Aggregate TopN Runtime Filter**，与 Databricks DFP、DuckDB/DataFusion Parquet Page Index 动态下推殊途同归。

---

## 五、编码、物化与内存布局：让数据以最紧凑形态流动

### 5.1 延迟物化：从扫描层走向分布式全局（GLM）

自 *Abadi et al. (ICDE 2007)* 奠定列存物化理论以来，延迟物化经历了三级跃迁：
1. **扫描层延迟物化**：先读过滤列算出位图/选择向量，只对命中行解压投影列（例如 TPC-H Q6 命中率仅 ~2%，几乎省掉 98% 的载荷列解码）。
2. **执行期逻辑延迟载体**：Meta Velox 用 `LazyVector` + `SelectivityVector` 让非过滤列穿过多个 Filter 甚至 Hash Join 探测后再按需物化，并在 `Dictionary / Constant / Sequence` 编码态直接算表达式。
3. **分布式全局延迟物化（Global Late Materialization, GLM）**：面对宽表 `ORDER BY ... LIMIT N`，Sort/TopN 与跨节点 Shuffle 中只携带排序键与 `(_tablet_id, _row_id)`，筛出最终 \(N\) 行后再精准回表点查，将宽表 Shuffle 内存与带宽压低一个数量级。

### 5.2 字符串双雄：German String vs 全局低基数字典

在 TPC-H 与 TPC-DS 中，字符串比较、哈希与搬运往往吃掉 **30%\~60%** 的 CPU 时间，现代引擎分化出两条互补高峰：

| 路线 | 代表引擎与论文 | 内存布局与核心机理 | 最优场景 | 局限与边界 |
| --- | --- | --- | --- | --- |
| **German String（16B 内联前缀字符串）** | **Umbra**（CIDR 2020 首创）、**DuckDB**、**Velox (`StringView`)**、**Arrow (`StringView`)** | 固定 16 字节：`[4B length][4B prefix][8B inline suffix 或 8B pointer]`；≤12B 短串完全内联零跳转；>12B 长串先比前 8 字节（length+prefix），99% 不等情况一条整型比较直接否决 | 通用变长字符串（无论高低基数）、无预先全局字典的湖仓外表、排序与前缀匹配 | 仍占 16B/行；极低基数重复维度列在 Shuffle 与哈希槽宽度上不如 4B 整型 |
| **全局低基数字典（Global Low-Cardinality Dict）** | **StarRocks**、**ClickHouse**、**Hologres**（列级字典） | 为低基数列维护字典编码，**把字符串列全程替换为定长 `int32`**，贯穿 Scan、Filter、HashJoin、HashAgg 与 Shuffle，直到最顶层才 `Decode` | 国家、品牌、品类、状态码等重复维度列：内存/Shuffle 缩减 4x–10x，哈希与比较退化为纯整型 SIMD | 高基数列（UUID、长文本）会字典膨胀失效；湖仓外部表需按块重映射或退化 |

**理想形态**：对低基数枚举维度走全局字典（`int32`），对中高基数变长字符串走 German String（`StringView`）；同时在列式内存所有权层采用侵入式引用计数的 **Copy-on-Write Column**（ClickHouse / StarRocks）或 `Buffer::isUnique()` 就地复用（Velox / Arrow），消灭防御性深拷贝。

---

## 六、执行微架构、Out-of-Core Spill 与 GPU 异构前沿

### 6.1 向量化 vs 编译执行的十年终局

自 **MonetDB/X100（CIDR 2005）** 确立批向量化执行、**HyPer（VLDB 2011）** 确立 LLVM 数据中心化代码生成以来，两派争论了十余年。*Kersten et al. (VLDB 2018)* 在同一框架下完成终局实证：**在 TPC-H 这类计算与访存并重的负载上，两派峰值性能极为接近**——编译执行在复杂算术表达式流水线融合上更省指令，但受困于编译延迟与调试复杂度；向量化则在显式 SIMD、批预取掩盖哈希探测内存延迟以及自适应/Spill 工程可控性上占优。

近年两派明显收敛：编译派 **Umbra** 引入 **Flying Start**（极速自研后端毫秒启动，大查询再异步切换 LLVM，详见[《Umbra 研究路线全景》]({{< relref "2026-09-04-dive-umbra.md" >}})）；大数据派 **Spark** 则借 **Gluten（Velox）** 与 **Comet（DataFusion）** 彻底走出 JVM GC 与弱 SIMD 天花板，全面转向 C++/Rust 原生向量化卸载。

### 6.2 Morsel-Driven 调度、哈希表微架构与大规模 Shuffle

- **Morsel-Driven 并行调度（*Leis et al., SIGMOD 2014*）**：用固定等于物理核数的 Worker 线程动态拉取细粒度 Morsel（Work Stealing）并保证 NUMA 本地亲和，配合异步非阻塞 Pipeline（I/O 或反压挂起立刻让出核），同时压住单机倾斜与高并发上下文切换。
- **Hash Join / Agg 四层微架构特化**：密集代理键（如 `d_date_sk`, `n_nationkey`）直接退化为数组直映（DuckDB `PerfectHash` / StarRocks `RangeDirectMapping`）；中型表采用 SIMD 1-Byte Tag 分组（SwissTable / Velox 128B Bucket）或指针高 16 位 Salt 挡掉 99%+ 假阳性；超 L3 巨表切入 Two-Level HashMap 与三阶段预取/协程 MLP，高基数局部预聚合配自适应 Bypass（完整源码剖析见[《OLAP 引擎里的 Hash Table》]({{< relref "2026-11-19-olap-hash-table-design-and-tuning.md" >}})）。
- **大规模网络 Shuffle 稳态治理**：在百机千核集群里，通过机内两阶段（Node-level 汇聚 + `LocalExchange` 保 1x 流量）、Broadcast 共享只读 HashTable、错峰发包 + Credit 背压 + DCTCP/TLP 根治 Linux PPS 墙与 TCP Incast（深入原理与开源/云数仓横评见[《大规模集群下的 Shuffle 稳定性与高性能架构》]({{< relref "2026-11-22-large-scale-shuffle-stability-and-performance.md" >}})）。

### 6.3 固态硬盘时代的 Out-of-Core Spill：消除“性能悬崖”

如 MaxCompute 100TB FDR 所示，用 8 TB 内存跑 100 TB TPC-DS，**能不能稳住不 OOM 且不出现性能断崖，是百 TB 引擎的生死线**。DuckDB 团队在 *ICDE 2024*（*"Robust External Hash Aggregation in the Solid State Age"*）与 *PVLDB 2025*（*"Saving Private Hash Join"*）中确立了现代 NVMe 时代的 Spill 范式（与 Velox / StarRocks 的演进高度一致，详参[《深入 Hash Join：从内存哈希表到分区 Spill》]({{< relref "2026-08-26-dive-hash-join.md" >}})）：
1. **Radix 分区渐进溢写替代粗暴回退**：内存吃紧时按 Radix 分区逐个将冷分区压缩列式落盘，热分区留内存，实现随内存缩减的**平滑线性降级**而非断崖崩溃；
2. **统一内存仲裁（Unified Buffer Pool / `MemoryArbitrator`）**：将持久数据缓存与临时算子内存纳入同一池子动态抢占回收，吃满 NVMe SSD 的高并发 IOPS 与带宽。

### 6.4 当 TPC-H / TPC-DS 遇上 GPU 异构计算

**Crystal（SIGMOD 2020）** 量化指出，GPU 加速分析查询的真正红利在于高出一个数量级的 **HBM 带宽**（TB/s 级）与极高线程并发掩盖访存延迟；**RAPIDS Spark（UCX GPU Shuffle）** 与 **Sirius（UW-Madison & NVIDIA, CIDR，复用 DuckDB 前端 + Substrait 下发至 `libcudf` + RMM）** 正推动组合式 GPU 执行落地。

但把 SQL 搬上 GPU 绝非银弹，必须跨过五道硬约束（详细拆解见[《CPU/GPU 异构计算中的 SQL/ETL 加速陷阱、算子设计与调度抉择》]({{< relref "2026-11-17-cpu-gpu-heterogeneous-sql-etl-survey.md" >}})）：**① PCIe 传输墙**（必须整段 Sub-Plan 驻留 HBM，严禁单算子来回拷）；**② 显存容量墙**（依赖 Pinned Host Memory 与 HBM→DRAM→NVMe 三级溢写）；**③ 控制流发散与长变长字符串**；**④ 小表 Kernel Launch 延迟**；**⑤ 跨介质 CBO 建模**（在 Cascades 中引入与 `Distribution` 并列的 `DeviceTrait` 物理属性，实现收益可算与安全 Fallback）。

---

## 七、内核工程师实战 Checklist：瓶颈定位与反例边界

面对一条跑慢的 TPC-H / TPC-DS 风格复杂查询，按下面这张“症状 → 机制 → 反例边界”表自顶向下排查，几乎能定位 90% 以上的根因：

| 观察到的瓶颈现象 | 优先检查的优化机制 | 必须警惕的反例与失效边界 |
| --- | --- | --- |
| **大表跨机 Shuffle 占满窄网卡** | Hash Clustering / Table Group Colocation（对齐 CU/Shard）+ 桶内 `SORTED BY`；维度表 1 Bucket + 节点级共享 Broadcast | 过度细碎的 `PARTITIONED BY × Bucket` 会引爆小文件与元数据，反而不如单一高基数分桶 + 桶内排序 |
| **同一大表被扫多遍** | 相关子查询转 Window（Q2/Q17）；CTE Reuse + 谓词/列并集下推（TPC-DS Q4/Q23/Q64） | 消费者谓词高度互斥或分布要求冲突时，强制 CTE 复用反而引发全扫与额外 Exchange |
| **多表 Join 中间结果爆炸** | Join Reorder（DPhyp/DP/UKFK）；直方图与多列统计；**Global Runtime Filter / DFP / Predicate Transfer**；AQE 动态转 Broadcast | Build 侧太大或过滤率极低时，RF 自身构建与探测开销会反噬；必须设体积门控与动态 Bypass |
| **大宽表或字符串查询 CPU 吃满** | **Global Low-Cardinality 字典** / **German String**；扫描与全局延迟物化（GLM）；表达式复用与谓词重排 | 高基数字符串建全局字典会膨胀退化；刚编码完立刻 Decode 纯属负收益 |
| **GroupBy / Rollup 慢或爆内存** | `PushDownAggregate`（先 Join Reorder 再决策）；GroupingSets 预聚合下推或改写 `UNION ALL`；Two-Level HashMap；Adaptive Streaming PreAgg | 高基数 GroupBy 强制 Local 预聚合会 1:1 透传并撑爆内存，必须允许自适应直通 |
| **TopN / Ranking Window 耗时高** | `PartitionTopN` 下推；`TopN / Agg TopN Runtime Filter`；重表达式上提；GLM 延迟回表 | `ORDER BY` 列无序且 `LIMIT` 很大时，TopN RF 收敛慢，需防背压拖慢 Scan |
| **多流并发或单节点长尾拖死 P99** | Morsel 动态切分 + `LocalExchange`；Skew Join（MCV/NULL 拆 Broadcast + Shuffle）；错峰发包防 Incast | 区分“分区倾斜”与“值倾斜”：单值热点靠盲目加并发反而让热点更尖 |
| **10TB–100TB 查询 OOM 或性能断崖** | Radix 分区渐进 Spill + 统一内存仲裁 + 压缩列式溢写 | Spill 只保不 OOM，不代表延迟可接受；优先靠分桶共置、RF 与聚合下推压小状态 |
| **尝试 GPU 加速却比 CPU 更慢** | 核查是否整段 Sub-Plan 驻留 HBM；检查 PCIe H2D/D2H 占比、数据规模与变长字符串/UDF 发散 | 小数据量、频繁 Fallback、非规整长字符串应坚决留在 CPU 向量化执行 |

---

> **公开基准与核心参考文献**
> - **TPC 官方公开榜单与 FDR**：
>   - Alibaba Cloud MaxCompute Query Accelerator 2.0 v5.57 — [TPC-DS 4.0.0 SF100000 (#5018)](https://www.tpc.org/5018) · [Official FDR PDF](https://www.tpc.org/results/fdr/tpcds/alibaba_cloud~tpcds~100000~alibaba_cloud_maxcompute~fdr~2026-08-19~v01.pdf)（2026-08-19）。
>   - Tencent Cloud Data Lake Compute (SuperSQL 1.0) — TPC-DS 4.0.0 SF100000（653,611,141 QphDS@100000GB，11.04 CNY/kQphDS）。
>   - Alibaba Cloud Hologres 4.1 — [TPC-H 3.0.1 SF3000 (#3414)](https://www.tpc.org/3414) · [Official FDR PDF](https://www.tpc.org/results/fdr/tpch/alibaba_cloud~tpch~3000~alibaba_cloud_hologres~fdr~2026-06-26~v01.pdf)（2026-06-29）。
>   - Alibaba Cloud Hologres V1.3 — [TPC-H 3.0.1 SF30000 (#3384)](https://www.tpc.org/3384) · [Official FDR PDF](https://www.tpc.org/results/fdr/tpch/alibaba~tpch~30000~hologres~fdr~2022_10_20~v01.pdf)（2022-10-31，已过期）。
> - **查询改写、优化器与信息旁路传递（SIP）**：Yan & Larson (*VLDB 1995*, Eager Aggregation) · Graefe (*1995*, Cascades) · Moerkotte & Neumann (*VLDB 2006* DPccp / *SIGMOD 2008* DPhyp) · Bellamkonda et al. (*PVLDB 2009*, Subquery-to-Window) · Neumann & Kemper (*BTW 2015*, General Unnesting) · El-Helw et al. (*PVLDB 2015*, Orca CTE) · Leis et al. (*PVLDB 2015*, JOB) · Neumann & Radke (*SIGMOD 2018*, Large Joins) · Yannakakis (*VLDB 1981*) · Putze et al. (*ACM JEA 2009*, Blocked Bloom) · Zhu et al. (*PVLDB 2017*, LIP) · Kandula et al. (*PVLDB 2019*, Data-Induced Predicates) · Yang et al. (*CIDR 2024* Predicate Transfer / *VLDB 2026* RPT+) · Birler et al. (*PVLDB 2024*, Diamond Hardened Joins).
> - **执行微架构、Spill 与 GPU 异构**：Boncz et al. (*CIDR 2005*, MonetDB/X100) · Abadi et al. (*ICDE 2007*, Materialization) · Neumann (*PVLDB 2011*, HyPer Codegen) · Leis et al. (*SIGMOD 2014*, Morsel-Driven) · Polychroniou et al. (*SIGMOD 2015*, SIMD) · Kersten et al. (*PVLDB 2018*, Compiled vs Vectorized) · Neumann & Freitag (*CIDR 2020*, Umbra & German String) · Shanbhag et al. (*SIGMOD 2020*, Crystal) · Pedreira et al. (*PVLDB 2022*, Velox) · Kuiper et al. (*ICDE 2024* External Hash Agg / *PVLDB 2025* Saving Private Hash Join) · UW-Madison & NVIDIA (*CIDR*, Sirius).

---
title: "Databricks、Snowflake 与 StarRocks 引擎内核横评：云原生架构、AI-SQL 范式与执行层取舍"
slug: "databricks-snowflake-starrocks-engine-and-aisql"
date: 2026-11-20T20:50:00+08:00
lastmod: 2026-11-20T20:50:00+08:00
categories:
  - 数据库
  - 分布式系统
tags:
  - Databricks
  - Photon
  - Snowflake
  - StarRocks
  - AISQL
  - 向量化执行
  - Runtime Filter
description: "从云原生数据平台的两条演进路径切入，拆解 Databricks（Photon、Delta 三件套与 Lakebase）与 Snowflake（三层存算分离、Gen2 与 Cortex AISQL）的技术护城河，剖析 AI/LLM 时代语义查询优化与 AI_JOIN 实现原理，并在 Vectorized、Pipeline 与 Runtime Filter（含 S3 布隆过滤下推）三个内核维度深度横评 Photon、Snowflake 与 StarRocks。"
draft: false
toc: true
math: true
---

把同一条星型模型多表关联查询放进 **Databricks Photon**、**Snowflake** 和 **StarRocks**，三套引擎往往都能跑出亚秒到秒级的亮眼成绩；可只要换一个负载——比如把查询改成百 TB 级长链路 ETL、高并发千 QPS 报表，或者在 `WHERE` 子句里塞进一句自然语言条件 `AI_FILTER('客户明确表达了退款意向', ticket_text)`——三套系统的延迟、账单和稳定性立刻会拉出数量级差距。

差距的根源不在表层 SQL 语法，而在三家对底层物理约束作出的不同下注：Databricks 选择把 C++ 向量化引擎嵌入 Spark JVM 生态并靠闭源智能运维构筑护城河；Snowflake 从第一天起就用洁净室 C++ 打造三层存算分离，把对象存储微分区裁剪做到极致；而开源实时 OLAP 标杆 StarRocks 则用 Morsel-Driven 异步流水线、全局低基数字典和自适应全局 Runtime Filter，把单机多核与实时交互压榨到极限。

进入 2025–2026 年，随着大语言模型推理直接下沉为数据库一等公民算子，这场引擎竞争又叠加了一重全新的物理变量——当单个算子的单行成本从 `10 ns` 暴涨到 `100 ms` 且按 Token 实时计费时，传统的向量化、流水线调度与代价优化器正在发生怎样的变形？

![Photon、Snowflake 与 StarRocks 执行内核与动态过滤链路对比](/images/diagrams/db-photon-snowflake-starrocks-kernels.svg)

---

## 一、两条云原生路径的竞争壁垒：Databricks vs. Snowflake

在钻进执行器细节之前，先看清 Databricks 与 Snowflake 在架构基因上的分叉。两者到 2026 年在产品形态上已经高度趋同，但各自的护城河依然牢牢长在原始底座之上。

### 1.1 Databricks：开放湖仓底座 + 闭源加速与平台智能

Databricks 的策略可以概括为“**文件格式与开源生态极度开放，计算加速与全局运维深度闭环**”：

- **Delta Lake 存储三件套定型**：通过 **Deletion Vectors（删除向量）** 将行级更新/删除从整文件重写推迟为轻量 RoaringBitmap 读时合并（小比例 `MERGE` 提速约 10 倍）；通过 **Liquid Clustering（液态聚类）** 取代刚性分区与全量重写的 `Z-ORDER`，以增量方式只重排未聚类文件并支持随查询轨迹自动选列（`CLUSTER BY AUTO`）；再借助 **UniForm + Apache Iceberg v3** 在二进制层面统一 Deletion Vectors 与 `VARIANT` 规范，用“一份 Parquet + 双份元数据”终结格式割裂。
- **Photon 执行引擎（SIGMOD 2022）**：以 C++ 重写核心向量化算子并通过 JNI 挂载在 Apache Spark 内部，既避开了 JVM GC 抖动与全阶段代码生成（WSCG）的编译泥潭，又无损继承了 Spark 庞大的数据处理与 ML 生态。
- **平台级闭环与版图延伸**：依托 **Unity Catalog** 对全平台查询模式的全局感知，**Predictive Optimization** 能自动触发统计采集、小文件合并与液态聚类——这是开源 Spark + Delta 用户无法复现的系统级红利。2025 年底随着收购 Neon 推出的 **Lakebase**（Serverless PostgreSQL）GA，Databricks 进一步把入口从下游分析向前延伸到了在线事务库与 Agent 状态存储。

### 1.2 Snowflake：洁净室 C++ 架构与极致多租户弹性

Snowflake 摒弃了 Hadoop/JVM 历史包袱，以三层彻底解耦的纯 C++ 架构（SIGMOD 2016 / NSDI 2020）树立了云数仓的工程标杆：

- **FoundationDB 驱动的全局服务层**：无状态 **Cloud Services Layer** 背后依托 FoundationDB 统一协调全局事务、元数据、裁剪索引与权限，使多个物理隔离的 **Virtual Warehouse** 可以在同一份远端对象存储上并发读写且互不干扰。
- **不可变微分区（Micro-Partition）与本地 SSD 缓存**：写入时自动按 50–500 MB（未压缩）切分微分区并维护丰富的列级 Min/Max 与 NDV 统计，配合一致性哈希路由的 **Ephemeral Local SSD Cache** 与 Multi-Cluster Warehouse 秒级扩缩，做到近乎零人工建索引/分区的“开箱即用”。
- **Gen2 Warehouse 与开放兼容**：2025–2026 年推出的 **Generation 2 Standard Warehouses** 结合新代硬件与内核重构，官方基准显示核心分析平均提速 **2.1x**、DML（`DELETE`/`UPDATE`/`MERGE`）最高提速 **4.4x**，并原生内建面向 Cortex AISQL 的容器化推理编排。在生态边界上，则通过 **Unistore Hybrid Tables** 补齐事务点查，通过托管与外部 **Apache Iceberg** 表回应开放湖仓诉求。

| 对比维度 | Databricks | Snowflake |
| :--- | :--- | :--- |
| **核心基因** | 开源计算与 ML 生态 + 闭源向量化加速与智能治理 | 纯自研 C++ 多租户数仓，极致零运维与隔离体验 |
| **最强壁垒** | Data+AI 全栈统一、Unity Catalog 预测优化、超大规模 ETL 韧性 | 多租户物理隔离、零调优微分区裁剪、极稳 BI 并发 |
| **存储与开放性** | Delta Lake 原生主场 + UniForm / Iceberg v3 双向兼容 | 自研 Micro-Partition 深度闭环 + Iceberg 原生支持 |
| **在线状态延伸** | **Lakebase**（Serverless PostgreSQL） | **Unistore Hybrid Tables**（行存事务 + 列存同步） |

---

## 二、AI / LLM 时代的引擎变形：语义算子与 `AI_JOIN` 原理

把大语言模型推理直接塞进 SQL，会立刻击穿经典数据库理论的三条隐含前提：

1. **单行代价暴涨 7 个数量级**：经典算子评估一行只需约 `10 ns` 且无边际货币开销；一次 LLM 推理耗时 `100 ms – 2 s` 且按 Token 实时计费。
2. **优化目标变成三维帕累托前沿**：传统 CBO 只需在确定性语义下最小化耗时与资源；语义算子具备概率性，优化器必须在**延迟、Token 财务成本与输出语义准确率**三者间寻找最优解。
3. **数据类型跨越纯标量**：原生 `FILE`（PDF、高清图像、音频）与 `VARIANT`（半结构化 JSON Shredding）成为表内一等公民（底层存储格式的对应变革可见 [AI 时代的大数据列存基座]({{< relref "2026-11-17-columnar-formats-orc-parquet-lance-in-ai-era.md" >}})）。

### 2.1 语义查询优化（Semantic Query Optimization）的四项内核改造

围绕 **Snowflake Cortex AISQL**（`AI_FILTER`、`AI_JOIN`、`AI_CLASSIFY`、`AI_AGG`）、**Databricks AI Functions**（`ai_query`、`ai_extract`）以及学术界的 **LOTUS（PVLDB 2025）** 和 **Palimpzest（CIDR 2025）**，现代优化器正在把四项全新机制写进 CBO Pass：

- **反直觉的谓词重排与上拉**：传统优化器总想把过滤条件下推到最底层扫描；但在混合关系/语义查询中，若把 `AI_FILTER` 排在 `date >= '2026-01-01'` 之前，或者错误下推到高淘汰率 Join 之前，会白白烧掉数百万次 LLM 调用。新代优化器同时在编译期与运行期监测选择率，强制先跑廉价确定性谓词，并按**预期总 LLM 调用次数**决定将 AI 谓词下推还是上拉越过 Join（Snowflake 披露其上下文重写与自适应路由可砍掉最高 **60%** 冗余 Token）。
- **自适应 Proxy \(\rightarrow\) Oracle 模型级联**：简单清晰的样本直接由轻量代理模型（Proxy，如向量距离或 7B 小模型）秒级裁决，仅将置信度处于灰区的模糊样本升级给昂贵的旗舰参考模型（Oracle），在保留 **90%–95%** 准确率的同时压缩绝大部分推理成本。
- **异步推理调度与前缀缓存**：CPU 向量化线程绝不能同步死等 HTTP 推理返回，引擎在底层解耦出异步微批队列与共享 System Prompt 的 Prefix KV Caching。

### 2.2 拆解 `AI_JOIN`：如何把 \(O(N \times M)\) 的语义连接压到近线性？

传统等值连接能从 \(O(N \times M)\) 降到 \(O(N + M)\)，靠的是等价哈希函数或全序排序；而 `JOIN ... ON AI_FILTER('该工单是否可由该知识库文章解决', t.desc, kb.doc)` 既无哈希也无全序，朴素嵌套循环在 \(10^4 \times 10^3\) 规模下就要发起千万次 LLM 调用。结合 Snowflake AISQL 与 LOTUS `sem_join` 的公开实现，引擎靠四步接力降维：

1. **按右表基数分流物理重写**：
   - 当 Build 侧（右表）基数较小（几十到几百条类别/规则/文档）时，Snowflake 会将 Build 侧整批打包进同一个 Prompt 上下文，把 `AI_JOIN` 重写为对 Probe 侧每行的**多标签分类 / 批选择**，直接将调用次数压至 \(O(N)\)（公开披露提速 **15x–70x**）。
   - 当左右表基数都很大时，退而采用 **Embedding Blocking**：为 Build 侧建向量索引（HNSW / IVF），对 Probe 侧每行只召回 Top-\(K\) 近似候选对，把候选空间从 \(N \times M\) 缩至 \(O(N \cdot K)\)。针对否定条件或长文本含噪导致原文 Embedding 失真的问题，LOTUS 与 SemJoin 还会先用轻量投影把原文规整为关键特征或合取范式再做粗筛。
2. **小样本校准双阈值级联**：从 Blocking 后的候选对中随机抽样跑一遍 Oracle 大模型得到参考标签，结合 Proxy 分数分布统计校准出高置信通过阈值 \(\tau_{\text{high}}\) 与高置信拒绝阈值 \(\tau_{\text{low}}\)（给出相对全量 Oracle 的 Precision/Recall 统计保证），仅把落入 \((\tau_{\text{low}}, \tau_{\text{high}})\) 灰区的候选对送去精判。
3. **混合关系裁剪先行**：若连接条件同时包含等值键与语义条件（如 `a.region = b.region AND AI_FILTER(...)`），先走传统 Hash 分桶把搜索域切碎，只在同桶内跑向量粗筛与语义精排。
4. **前缀聚簇微批投递**：把送往推理池的灰区候选对按共享 System Prompt 或固定右表文本分组拼包，放大底层推理引擎（如 vLLM / SGLang / Arctic Inference）的 Prefix KV Cache 命中率。

---

## 三、执行内核硬核横评：Photon vs. Snowflake vs. StarRocks

剥开商业叙事回到数据库内核本身，Photon、Snowflake 与 StarRocks 在 **Vectorized 向量化**、**Pipeline 流水线调度**与 **Runtime Filter 动态过滤**三个核心支柱上，各自踩中了不同的甜蜜点与工程暗礁。

### 3.1 向量化执行：Position List、微分区闭环与全局低基数字典

| 内核维度 | Databricks Photon (SIGMOD 2022) | Snowflake (SIGMOD 2016 / Gen2) | StarRocks (Vectorized C++) |
| :--- | :--- | :--- | :--- |
| **运行态宿主** | C++ 引擎经 **JNI** 嵌入 Spark JVM | 全栈 C++ 从零构建，零 JVM 依赖 | 全栈 C++ 从零构建（BE / CN） |
| **批处理单元** | `ColumnBatch` 连续列内存块 | 紧耦合微服务/微分区的列块 | `Chunk`（默认 4096 行，可配 `vector_chunk_size`） |
| **过滤活跃行表示** | **Position List**（显式活跃行下标数组，免物理拷贝） | 向量化裁剪 + 微分区底层跳过 | 过滤位图 + 延迟物化 + 部分路径物理 Compaction |
| **编码态加速王牌** | 针对开放 Parquet 的 SIMD 批解码 | 闭源 Micro-Partition 端到端编码协同 | **低基数全局字典**：字符串转定长整型直算 Join/Agg/Sort |

- **Photon 的得与失**：SIGMOD 2022 论文验证了一个经典工程判断——在稀疏过滤场景下，维护显式活跃行下标数组（**Position List**）比稠密位图或反复物理拷贝压缩列块省下大量 CPU 周期，后续算子可以直接跳过被过滤行。但它的阿喀琉斯之踵同样明显：作为 Spark JVM 的加速插件，一旦遇到未覆盖的表达式、UDF 或复杂嵌套类型就会触发 **Fallback 回退**，在 JNI 边界付出高昂的行列转换与堆内外拷贝税；面对开放 Parquet 也缺少跨文件强托管的全局字典红利。
- **Snowflake 的得与失**：全栈纯 C++ 彻底消灭了 Fallback 断崖，而且存储格式（Micro-Partition）与执行器高度一体化，Gen2 进一步强化了 DML 与复杂算子的硬件亲和。代价是这些极限优化与闭源内部微分区强绑定，查询外部 Iceberg 表时部分内部编码优势难以完整复现。
- **StarRocks 的得与失**：在字符串密集型 BI 与多维聚合中，StarRocks 的**低基数全局字典优化（Low-Cardinality Optimization）**具备极强杀伤力——CBO 在物理重写阶段直接把低基数字符串或数组列映射为定长整数 ID，让最耗 CPU 的字符串哈希探测、分组、排序与内存拷贝全部变成整型 SIMD 运算，全程免解码；配合 Native Parquet/ORC Reader 的 **Late Materialization**（先扫谓词列生成 `rowid bitmap` 再按需物化其余列）与 Page Index 裁剪，湖仓直查同样凶悍。短板在于部分算子路径在过滤后采用物理 Compaction 拷贝 Chunk，在连续多过滤条件下内存搬移多于 Photon 的 Position List；同时在大规模 Iceberg Equality Delete（实现为 Left Anti-Join）与高频 Primary Key 表 Compaction 场景下，jemalloc 内存峰值与写放大仍是社区持续打磨的痛点。

### 3.2 流水线与调度模型：Pull + Stage 容错 vs. Push Work-Stealing vs. Morsel 状态机

- **Photon（Pull-based 向量化 + Spark Stage / AQE）**：算子间保持 `next()` 向上拉取的控制流，寄生于 Spark 的 Stage 级落盘 Shuffle 与自适应执行（AQE）之上。这让它拥有三者中最强的**百 TB 级长耗时批处理容错韧性**——运行两小时中途挂掉几台节点只需局部重算受损分区，且 AQE 可在 Stage 间隙合并碎分区或拆分倾斜桶。但 Stage 落盘屏障天生拉高了小查询延迟，Pull 模型在深层算子树里的指令缓存局部性也逊于 Push 模型。
- **Snowflake（Push-based + 跨节点动态 Work-Stealing）**：生产者主动向下游推送列块，让热数据紧贴 CPU 寄存器与 L1/L2 Cache，天然适配 DAG 分叉计划；NSDI 2020 论文中极具巧思的**跨节点 Work-Stealing** 允许提前跑完本地微分区的计算节点动态窃取慢节点的剩余任务，在云端 S3 尾部抖动和数据倾斜时把 P99 延迟压得极稳。不过纯 MPP 内存流水线面对超长超大批 ETL 时的局部容错弹性仍弱于 Spark Stage 落盘模型。
- **StarRocks（Morsel-Driven Pipeline 状态机 + IO/CPU 彻底分离）**：将 Fragment 拆为 `Source -> Operators -> Sink` 的 Pipeline，由 **Driver 状态机**以 Morsel 微块为粒度在固定 CPU 线程池内协作抢占调度，彻底告别“每查询开几十线程”的上下文切换风暴；更关键的是在 Shared-Data 存算分离模式下将 **CPU Task 与 Async IO Task 严格剥离**——当 `ScanOperator` 发起远端对象存储读取时提交异步 IO 任务并立刻切入 `PENDING_CHUNK` 释放 CPU 线程，下游满载时切入 `PENDING_FINISH`/`INPUT_EMPTY` 自然传导背压。这使 StarRocks 在**高并发亚秒级 BI 场景的吞吐与性价比上表现突出**。其代价在于复杂的异步状态机叠加上 **Spill to Disk** 后边界错误路径陡增（社区曾出现 Hash Join Spill 异常状态未同步导致 Probe 侧死循环的典型竞态），且 FE 元数据层长期背负的 BDBJE 在超大规模 Multi-Warehouse 并发写入与千万级 Shard 调度上，明显不如 Snowflake 基于 FoundationDB 的云服务层从容。

### 3.3 Runtime Filter 与云存储 S3 I/O 裁剪的四道漏斗

在星型/雪花模型多表关联中，动态运行时过滤（Runtime Filter, RF）直接决定云存储网络带宽是省掉 90% 还是白拉全表。这里容易混淆两个概念：**静态存储侧布隆过滤器**用于回答“单个明确值 `x` 在不在这个文件/RowGroup 里”；而 Join Build 侧临时构建的**动态 Runtime Bloom Filter** 因长度与哈希种子和存储侧不同，无法直接与静态 BF 按位求交，必须借由以下四道漏斗层层削减 S3 `GET` 请求：

1. **文件级跳过（零 S3 数据文件 `GET`）**：
   - **Snowflake `JoinFilter`**：Build 侧提取的过滤界限或集合直接下推到 Probe 侧的 **Micro-Partition Pruning**，在向 S3 发第一个字节请求前就能剪掉 95%+ 的无关微分区（前提是微分区按相关键聚簇良好，否则 Min/Max 重叠会导致裁剪衰减）。
   - **Photon + Delta DFP（Dynamic File Pruning）**：将小维表产出的运行时键值/范围注入 `_delta_log` 文件级统计，直接剔除无关 Parquet 文件；针对高基数 UUID 等值点查，Delta 还曾提供 `_delta_index` 旁路布隆索引文件，但在 DBR 12.2+ / Photon 时代因写放大较重，已逐步演进为 **Liquid Clustering + Predictive I/O**。
2. **Row Group 级跳过（KB 级 `Range GET` 跳过几十 MB 数据块）**：
   - 拉取 Parquet 尾部 Footer 后，静态等值谓词可直接拉取几十 KB 的 Parquet 原生 Split Block Bloom Filter 探测跳过整块 Row Group；
   - **Runtime Bloom Filter 省 S3 I/O 的杀手锏在于字典页过滤**：当 Probe 侧 Join Key 列采用字典编码时，Reader 只需用一次极小的 S3 `Range GET` 拉取几 KB 到几十 KB 的 **Dictionary Page**，把字典内全部唯一值送入 Runtime Bloom Filter 探测——只要无一命中，该 Row Group 的全部 Data Pages 直接免下载。
3. **列级与 Page 级延迟物化（砍掉宽表其余 95% 列带宽）**：
   - 即使 Row Group 命中少量行，引擎也只先拉取 Join Key 单列并用 Runtime Bloom Filter 过滤（Photon 缩减 Position List，StarRocks 生成 `rowid bitmap`），再结合 Parquet **Column/Offset Index** 仅对包含存活行的少数 Page 按精确字节区间拉取宽表其余非 Key 列，往往直接削减 **80%–95%** 的下载流量。
   - StarRocks 在这一层还做到了极高的工程完备度：按 NDV 自动选用 **In-list / Bloom Filter / Min-Max**，通过 FE 协调跨 Fragment 分发 **Global Runtime Filter（GRF）** 并区分 Early RF 等待与 Late RF 防阻塞降级，还具备**运行期自适应裁撤**能力——实时监测到某个 RF 过滤率太差白耗 CPU 时自动关闭。
4. **对象存储请求合并（IO Coalescing）**：
   - 鉴于 S3 单次 HTTP `GET` 存在数十毫秒固定延迟且按请求次数计费，Photon、StarRocks 与 Arrow/Iceberg Reader 在算出零散命中区间后，都会把相距较近（如 `< 1 MB`）的 Range 合并成一次连续 `Range GET`，防止“省了带宽却打爆请求延迟”。

---

## 四、给 OLAP 内核与平台选型的四点落脚点

综合三家架构演进，无论做技术选型还是规划开源 OLAP 内核路线，边界其实非常清晰：

1. **场景选型不走极端**：重度 Data+AI/ML 与百 TB 级长链路批 ETL，**Databricks**（Photon + Liquid Clustering + `ai_query`）综合上限最高；多部门共享数仓、追求极低运维心智与开箱即用 AISQL，**Snowflake**（Gen2 + Micro-Partition + Cortex AISQL）最为省心；追求极致性价比的高并发实时 BI、亚秒级湖仓直查与自主可控开源内核，**StarRocks**（Morsel Pipeline + 全局字典 + 自适应 GRF）依然不可替代。
2. **异步 Pipeline 引擎天生适合吸收 AISQL**：像 StarRocks 这样已经完成 **Async IO Task 与 CPU Task 分离** 的执行内核，只需要把远程 LLM/Embedding 推理抽象为新型异步微批算子，并在 CBO 中引入基于 Token 成本的谓词重排与模型级联（详见姊妹篇 [AI Functions 与现代 LLM 数据管线深度调研]({{< relref "2026-11-20-ai-functions-and-llm-data-pipelines.md" >}})），就能平滑跨入语义查询时代。
3. **补齐数据布局自动化与 Position List**：低基数全局字典在高基数文本与 `VARIANT` Shredding 面前优势会收敛，引入类似 Photon 的 Position List 降低中间物化拷贝，以及对标 Liquid Clustering 的后台自动聚簇，是开源湖仓引擎下一步缩小运维体验差距的关键。
4. **元数据与多租户控制面必须轻量化解耦**：云原生多租户竞争的下半场，瓶颈往往不在单核 SIMD 而在千万级 Shard 调度与高频事务并发——用分布式 KV 替代单点重型元数据存储（如 BDBJE），是开源存算分离架构迈向超大规模 Multi-Warehouse 必须跨越的门槛。

---

> **参考资料与公开信息基线**
> - **Databricks & Photon**：Alexander Behm et al., *Photon: A Fast Query Engine for Lakehouse Systems* (SIGMOD 2022) · Databricks 官方文档与工程博客（Delta Lake Deletion Vectors、Liquid Clustering、UniForm & Iceberg v3、Dynamic File Pruning、Databricks SQL `ai_query`、Lakebase）。
> - **Snowflake**：Benoit Dageville et al., *The Snowflake Elastic Data Warehouse* (SIGMOD 2016) · Midhul Vuppalapati et al., *Building An Elastic Query Engine on Disaggregated Storage* (NSDI 2020) · Snowflake 官方文档（Generation 2 Standard Warehouses、Dynamic Join Pruning、Cortex AISQL & AI-aware Query Optimization）。
> - **语义算子与前沿论文**：Liana Patel et al., *Semantic Operators and Their Optimization: Enabling LLM-Based Data Processing with Accuracy Guarantees in LOTUS* (PVLDB 2025) · Chunwei Liu et al., *Palimpzest: Optimizing AI-Powered Analytics with Declarative Query Processing* (CIDR 2025)。
> - **StarRocks**：StarRocks 官方架构文档与工程博客（Pipeline Execution Engine、Global Runtime Filter、Low-Cardinality Optimization、Shared-Data Architecture）。

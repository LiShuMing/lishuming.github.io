---
title: "CPU/GPU 异构计算中的 SQL/ETL 加速陷阱、算子设计与调度抉择"
slug: "cpu-gpu-heterogeneous-sql-etl-survey"
date: 2026-11-17T22:30:00+08:00
lastmod: 2026-11-17T22:30:00+08:00
categories:
  - 数据库
  - 异构计算
tags:
  - GPU
  - RAPIDS cuDF
  - Sirius
  - DuckDB
  - 执行引擎
  - 调度与优化
description: "从第一性原理拆解为什么 SQL/ETL 上 GPU 往往跑不过 CPU，深入剖析 RAPIDS cuDF（libcudf）、Sirius 与自研 GPU 算子如何加速 Hash Join 与 GroupBy Aggregate，以及混合引擎如何在 CPU 与 GPU 之间做调度与回退决策。"
draft: false
toc: true
---

把一套 SQL 或 ETL 流水线从 CPU 搬到 GPU，直觉上应该快上一个数量级：现代数据中心 GPU 拥有数 TB/s 的 HBM 显存带宽（如 H100/GH200 约 3 TB/s）和上万个 CUDA 核心，而多核服务器 CPU 的 DDR5 内存带宽通常在 200–400 GB/s 量级。但在实际工程中，许多团队把 Pandas、Spark 或 DuckDB 的任务接入 GPU 后，却经常发现端到端耗时几乎没有下降，甚至比纯 CPU 更慢。

要理解这个反直觉现象，不能只看 GPU Kernel 自身的峰值算力，而必须把**算术强度（Arithmetic Intensity）、PCIe 数据搬运成本、SIMT 执行模型与异构调度边界**放在同一笔账里算清楚。

---

## 一、为什么很多 SQL/ETL 上 GPU 反而更慢？

### 1.1 先算一笔物理账：算术强度与 PCIe 税

深度学习矩阵乘法（GEMM）在 GPU 上能轻松获得几十倍加速，是因为它的**算术强度**极高——每个字节从显存读入后，会被反复复用做上百次乘加运算，瓶颈在算力（Compute-bound）。

关系代数与 ETL 算子恰恰相反。以一个典型的过滤聚合查询为例：

```sql
SELECT dept, SUM(salary)
FROM employees
WHERE age > 30
GROUP BY dept;
```

每读入一行数据（比如 16 字节），GPU 只做一次比较和一次加法，算术强度不到 0.2 FLOP/Byte，是一个彻底的 **Memory-bound（内存带宽受限）** 任务。此时决定上限的不是 CUDA 核心数，而是**数据从哪里读、经过哪条总线**。

假设数据最初在主机内存（Host RAM）中，规模为 1 GB：

1. **PCIe 传输耗时**：PCIe 4.0 x16 的理论双向峰值约 32 GB/s，实际有效吞吐通常在 20–26 GB/s。把 1 GB 数据从 Host 拷贝到 Device（H2D）需要约 **40 ms**；如果算完还要把结果（或中间表）拷回 Host（D2H），又要再付一次拷贝时间。
2. **GPU 计算耗时**：在 3 TB/s 带宽的 HBM 上扫描并聚合 1 GB 数据，Kernel 本身只需要 **0.3–1 ms**。
3. **CPU 对比耗时**：现代向量化引擎（如 DuckDB、Polars）在 300 GB/s 的 DDR5 上直接原地执行，无需跨总线拷贝，整个查询往往 **5–10 ms** 就结束了。

算完这笔账就能明白：**当数据必须经由 PCIe 往返搬运，而流水线又不够长时，GPU 省下来的几毫秒计算时间，根本填不平 40 ms 的总线传输税**。

### 1.2 四个最常见的“负加速”陷阱

除了 PCIe 带宽墙，SQL/ETL 在 GPU 上跑输 CPU 通常还会撞上以下四个工程约束：

| 陷阱类型 | 底层硬件/执行约束 | 典型症状 |
| :--- | :--- | :--- |
| **小数据集与启动延迟** | CUDA Kernel 启动延迟约 5–20 μs；RMM 内存池初始化、Python/JVM 到 Arrow/C++ 的桥接开销不可忽略 | 几万到几百万行的数据在 CPU 上全在 L2/L3 Cache 内完成；GPU 大量线程束（Warp）处于空转状态 |
| **变长字符串与嵌套类型** | GPU 采用 SIMT（单指令多线程）模型，要求同一 Warp 内 32 个线程执行相同路径、连续访存 | 正则匹配、长短不一的 `VARCHAR` 拼接、`List`/`Struct` 嵌套列引发严重的**线程束分化（Warp Divergence）**与非合并访存 |
| **数据倾斜与原子争用** | 多个线程并发更新同一聚合桶时，必须依赖全局显存上的 `atomicAdd` / `atomicCAS` | 低基数（Low Cardinality）或严重倾斜的 `GROUP BY` 键导致大量线程在同一个显存地址上排队串行化 |
| **CPU↔GPU 频繁横跳** | 算子覆盖率不足或显存 OOM 触发回退（Fallback），导致中间表在 Host 与 Device 之间反复拷贝 | 一次查询包含 1 个不支持的 UDF，前 3 个算子在 GPU 省了 5 ms，回退拷贝赔掉 50 ms |

此外，在端到端 ETL 中还有**木桶效应（Amdahl's Law）**：如果上游 Parquet/CSV 解压缩、网络读取或下游写盘仍卡在单机 CPU 或磁盘 I/O 上，把中间计算加速 20 倍，整体耗时也只能缩减一小半。

---

## 二、cuDF、Sirius 与自研算子是如何实现 GPU 加速的？

既然搬运很贵、线程分化很致命，真正能跑赢 CPU 的 GPU 分析引擎（以 RAPIDS `libcudf`、UW-Madison 与 NVIDIA 联合推出的 `Sirius`，以及各类自研 CUDA/Triton 算子为代表）必须围绕**“减少搬运、对齐访存、融合内核、控制显存”**四件事做到底。

### 2.1 显存驻留与零拷贝直通（Residency & GPUDirect）

要把 HBM 的带宽红利兑现出来，核心前提是**让整个计算管线尽可能驻留在显存中（GPU-Resident）**，而不是每算完一个算子就拷回 Host：

- **管道级驻留**：从 `Scan -> Filter -> Join -> Aggregate -> Sort` 全部在 Device 内存指针上接力，只有最终结果集（往往经过过滤和聚合后缩小了几个数量级）才回传给 Host。
- **I/O 绕过 CPU**：利用 **GPUDirect Storage (GDS)** 让 NVMe SSD 通过 DMA 直接将数据送入显存，配合 **nvCOMP** 在 GPU 上直接完成 Parquet/ORC 的 Snappy/ZSTD/LZ4 并行解压，避免 CPU 解压后再走 PCIe 搬运。
- **统一内存架构的物理红利**：在 NVIDIA Grace Hopper（GH200）等通过 **NVLink-C2C** 互联的平台上，CPU 与 GPU 共享高达 900 GB/s 的一致性互联带宽，显著削弱了传统 PCIe 的传输墙。

### 2.2 面向 SIMT 的列式内存布局（Apache Arrow / libcudf）

GPU 极度排斥行存（Row-oriented）的跨步访存。如果一行有 64 字节，一个 Warp 的 32 个线程同时读取同一列，行存会导致 32 次分散的缓存行加载；而采用与 **Apache Arrow** 兼容的**列式内存布局**后，同一列的 32 个连续 `int32` 恰好落在连续的 128 字节内，硬件可以一次完成**合并访存（Coalesced Memory Access）**：

- **定长列**：连续的值数组 + 独立的位图（Validity Bitmask，表示 `NULL`）。
- **变长字符串列**：拆分为定长的 `offsets` 偏移数组（`int32`/`int64`）与紧凑拼接的 `chars` 字节缓冲区，避免在显存里存放指针数组。

### 2.3 并行原语与算子融合（CUB / cuCollections + NVRTC JIT）

在工程实现上，无论是 `libcudf` 还是基于它的 `Sirius`，都不会从零手写所有底层并发原语，而是构建在 NVIDIA CUDA-X 原语生态之上：

1. **基础并行原语（CUB / Thrust）**：
   - `DeviceRadixSort`：基数排序，纯访存驱动且无比较分支，是 GPU 上最快的排序原语。
   - `DeviceScan`（前缀和）：GPU 算子通常无法像 CPU 那样一边过滤一边 `std::vector::push_back`，因此普遍采用**两阶段输出分配（Two-Pass）**——第一遍统计每个线程命中的行数，通过前缀和算出全局偏移量并一次性分配输出显存，第二遍并行写入。
2. **内核融合与即时编译（Kernel Fusion & JIT）**：
   - 如果一个复杂表达式 `WHERE (a + b) * c > 10 AND d < 5` 拆成 4 个独立 Kernel 执行，中间结果必须反复写回 HBM 再读出，白白浪费 3–4 倍显存带宽。
   - `libcudf` 通过 **NVRTC（NVIDIA Runtime Compilation）** 与 AST 表达式求值器将多个逐元素算子（Filter + Project）动态编译融合为单个 CUDA Kernel，让中间值全程留在寄存器（Register）内；自研算子则常借助 CUDA C++ 模板融合或 OpenAI Triton / PTX 代码生成来消除中间物化。

### 2.4 显存池化与三级溢出管理（RMM & Sirius）

原生的 `cudaMalloc` / `cudaFree` 会触发全局设备同步，在高频分配临时缓冲区的 SQL 引擎中会直接拖垮并发。因此 `libcudf` 强制使用 **RMM（RAPIDS Memory Manager）** 维护设备端异步内存池（如 `cuda_async_memory_resource` / `pool_memory_resource`）。

而在显存容量受限（通常几十 GB）的问题上，**Sirius**（CIDR 2026）在 `libcudf` 与 RMM 之上进一步构建了**三级分层内存管理器（Tiered Memory Management）**：

1. **GPU Tier**：高速 HBM 显存池，存放活跃计算分块。
2. **Host Tier**：NUMA 本地的锁页主机内存（Pinned Host Memory），作为显存饱和时的降级缓冲池，避免直接崩溃。
3. **Disk Tier**：磁盘溢出池（`idisk_io_backend`），作为最后的 Spill 兜底。

---

## 三、核心算子深度拆解：Join 与 Aggregate 如何在 GPU 上加速？

在关系型数据库中，Scan 和 Filter 相对容易并行化，真正决定执行引擎成败的是需要跨行数据重组与并发写冲突协调的 **Join** 与 **GroupBy Aggregate**。

### 3.1 Hash Join：`cuco::static_multimap`、Cooperative Groups 与延迟物化

在 `libcudf`（以及调用它的 `Sirius`、`cudf-polars`）中，非排序输入的等值连接主要采用基于 **`cuCollections`（`cuco`）** 的并行 Hash Join，其核心流程分为三步：

1. **Build 阶段（构建 `cuco::static_multimap`）**：
   - 选取较小的表作为 Build 侧，将其 Join Key 列的哈希值与行号（Row Index）写入 GPU 显存中的定长并发哈希表 `cuco::static_multimap`。
   - 之所以必须用 **Multimap**，是因为数据库 Join 普遍存在一对多或多对多匹配（同一个 Key 对应多个行号），采用开放寻址（Open Addressing）配合线性/双重哈希探测，能够把所有 Key-Index 对紧凑地铺在连续显存中，避免链表指针追逐。
2. **CUDA Cooperative Groups 协同探测（Probe 阶段）**：
   - 传统设计让 1 个 CUDA 线程负责 1 行数据的哈希与探测。但在多列复合 Key、变长字符串 Key 或高冲突场景下，单线程需要做多次跨列比较，极易引发 Warp 内线程分化。
   - `cuCollections` 引入了 **CUDA Cooperative Groups**（如 `cg::thread_block_tile<32>` 或更小的 Sub-warp Tile），让一组线程（例如 4 个或 8 个线程）协同完成同一行的哈希计算、开放寻址槽位加载与多列相等性比较。组内线程以统一的步调读取相邻槽位，既充分利用了合并访存，又大幅压低了分支分化。
3. **两阶段 Gather Map 与延迟物化（Late Materialization）**：
   - Probe 阶段**不直接拷贝整行 Payload 数据**，而是先通过前缀和统计匹配数量，输出两列仅含 `int32` 行号的 **Gather Maps（`left_indices`, `right_indices`）**。
   - 最后调用高度优化的并行 `gather` Kernel，根据索引映射表把两张表的 Payload 列一次性拼装输出。这种做法让核心哈希表只存 8–16 字节的 Key/Hash 与 Row ID，极大幅度降低了 HBM 占用与随机写带宽压力。

> **当 Hash Join 遇到显存局部性瓶颈或有序输入**：如果 Build 表极大、随机探测超出 GPU L2 Cache 容量，自研引擎常会引入 **Radix Partitioned Hash Join**（先用基数分区把左右表切进共享内存或 L2 友好的小分区，再局部 Build/Probe）；而当输入已经有序、或内存极度紧张无法容纳哈希表负载因子（Load Factor）时，则会切换为 **Sort-Merge Join**（`CUB::DeviceRadixSort` + 并行 Merge-Path / 二分查找）。

### 3.2 GroupBy Aggregate：Hash-Based vs Sort-Based 与原子争用治理

GPU 上的 `GROUP BY` 聚合主要有两条技术路线，各自对应截然不同的工作负载边界：

| 维度 | Hash-Based GroupBy | Sort-Based GroupBy |
| :--- | :--- | :--- |
| **底层机制** | 对 Group Key 哈希并写入并发哈希表，通过 `atomicAdd` / `atomicMin` / `atomicCAS` 累加聚合状态 | 先用 `CUB::DeviceRadixSort` 按 Key 排序使相同组相邻，再执行 `reduce_by_key` 分段归约 |
| **访存特征** | 随机显存访问 + 硬件原子指令 | 100% 顺序合并访存（Coalesced），无原子写冲突 |
| **计算复杂度** | `O(N)` | `O(N log N)`（基数排序为 `O(N · k)`，`k` 为 Key 比特位宽） |
| **适用与退化边界** | 适合中等基数、数值型聚合；**低基数或重度倾斜时因原子争用严重退化** | 适合高基数、**嵌套/变长复杂类型**（如 `List`/`Struct`）、或需要有序输出（如窗口函数、`NUNIQUE`）的场景 |

**原子争用的工程解法（基数悖论与分级预聚合）**：
在直觉上，分组越少（基数越低），聚合似乎应该越快；但在 GPU Hash-Based GroupBy 中却常出现**“基数悖论”**——当几千万行数据全部映射到 10 个分组时，成千上万个线程会同时向全局显存里的那 10 个地址发起 `atomicAdd`，硬件原子单元被迫将并发请求串行化，吞吐断崖式下跌。

为此，`libcudf` 与自研 GPU 算子普遍采用**三级归约（Block/Warp-Local Pre-aggregation）**：
1. **Warp 级归约**：利用 `__shfl_down_sync` 洗牌指令在寄存器内合并同一 Warp 中相同 Key 的增量。
2. **Thread-Block 共享内存预聚合**：利用片上高速 `__shared__` 内存（延迟比全局 HBM 低两个数量级）建立局部哈希表或局部计数器，将整个线程块几百个线程的更新折叠为少数几次写回。
3. **全局原子提交**：每个 Block 仅向全局显存发起一次 `atomicAdd`，将全局原子争用降低 1–2 个数量级。当 Key 类型复杂或基数极高时，引擎则会直接切回 Sort-Based 路径。

---

## 四、CPU 与 GPU 之间是如何做调度与回退抉择的？

在真实生产环境中，没有任何一个 GPU 引擎能 100% 覆盖 SQL 标准或 Pandas API 的所有角落。因此，异构系统的核心竞争力往往不在单个 Kernel 多快，而在于**如何决定“谁来算”以及“何时安全回退”**。目前业界主要有四种典型的调度范式。

### 4.1 透明代理与试错回退：`cudf.pandas`

`cudf.pandas` 的目标是让用户“零代码改动”加速现有 Pandas 脚本。它的调度逻辑建立在**动态代理对象（Proxy Object）**之上：

- **GPU-First Fast Path**：所有 `DataFrame` / `Series` 都被代理对象包装。每次方法调用时，优先在 GPU 上尝试调用 `cuDF` 实现；如果底层数据还在 CPU，则透明执行 H2D 拷贝。
- **异常捕获与回退（Slow Path）**：一旦遇到 `cuDF` 尚未支持的 API、参数签名差异或触发 GPU OOM，代理层捕获异常，将状态同步回 Host（D2H），并在标准 `pandas` 上重放执行。

这种“先试再退”的机制兼容性极强，但也带来了最隐蔽的性能杀手：**试错税（Trial-and-Error Penalty）与 PCIe 乒乓**。如果在循环热路径里反复调用一个不支持的 Pandas 方法，每次都会经历“准备 GPU -> 抛异常回退 -> D2H 拷贝 -> CPU 执行 -> 下一步再 H2D”的灾难循环。工程上必须通过开启 `LOG_FAST_FALLBACK=True` 排查回退点，或用 `.as_gpu_object()` 显式脱离代理层。

### 4.2 算子级计划改写与边界代价：`spark-rapids` 与 `cudf-polars`

在具有明确逻辑/物理执行计划（IR / Catalyst Plan）的引擎中，调度决策前移到了**计划编译期**：

- **RAPIDS Accelerator for Apache Spark (`spark-rapids`)**：
  - 通过扩展 Spark Catalyst 优化器规则，遍历物理计划树，将支持的算子替换为 `GpuFileScan`、`GpuHashJoin`、`GpuHashAggregate` 等，不支持的算子保留在 CPU。
  - 在 CPU 与 GPU 算子的交界处，自动插入 **`HostColumnarToGpu`** 或 **`GpuColumnarToHost`** 转换节点，并由离线工具 **Qualification Tool** 读取历史 Event Log 预估 PCIe 拷贝与加速收益（ROI），辅助决定作业是否值得上 GPU。
  - 节点间 Shuffle 通过 **RAPIDS Shuffle Manager** 结合 UCX / RDMA 实现 GPU-to-GPU 直传，绕过主机内存。
- **`cudf-polars`（Polars GPU Engine）**：
  - 当用户调用 `.collect(engine="gpu")` 时，遍历 Polars LazyFrame 的优化后 IR 图，检查所有节点、表达式和类型是否受 `libcudf` 支持；早期版本一旦发现不支持节点即整条查询回退到 Polars CPU，新版本则结合 **RapidsMPF** 流式运行时（及 `RayEngine` / `DaskEngine` / `SPMDEngine`）支持更大规模流式切分与多卡并行。

### 4.3 GPU-Native 可组合引擎与“CPU 优化器错配”：`Sirius`

与逐算子拼接不同，**Sirius** 采用了 **GPU-Native Composable** 路线：不重写 SQL 解析器和优化器，而是直接复用 DuckDB（或 Apache Doris）前端，通过开源计划中间表示 **Substrait** 接管物理计划，并在底层由 `libcudf` + 三级内存管理器统一执行：

![Sirius：DuckDB 内嵌 GPU 原生分析执行与自动回退链路](/images/diagrams/dbres-sirius-gpu-execution.svg)

在调度与回退上，Sirius 的核心策略是**整段管线优先下推 + 能力边界安全回退**：
- 如果 Substrait 计划中的所有算子、数据类型和标量函数均受 GPU 支持，则整条查询全程在 GPU HBM（必要时经三级缓冲池溢出）中完成，彻底避免算子间的 PCIe 往返。
- 若存在复杂嵌套类型、特定正则回溯或未实现算子，则将该子计划或整条查询透明交还 DuckDB 原生 CPU 引擎执行。

不过，Sirius 在工程和论文中也暴露了一个极具代表性的深层问题——**继承 CPU 代价模型的错配（Inherited CPU Cost Model Mismatch）**：
DuckDB 生成的 Join 顺序、Build/Probe 侧选择以及物化策略，是基于 CPU 缓存局部性、分支预测和单核指令吞吐估算出来的。这套“CPU 最优计划”喂给拥有几万个并发线程和 TB/s 显存带宽的 GPU 时，可能并非最优（例如 CPU 倾向于把极小的过滤推迟或选择不同的 Join 树形）。构建原生的 **GPU-Aware Cost Model**（显式建模 HBM 容量、核函数启动与溢出代价），正是当前异构查询优化的前沿方向。

### 4.4 自研异构调度器的四条实用决策准绳

如果团队正在自研 CPU/GPU 混合执行引擎，调度器在决定一个子计划或算子是否下发到 GPU 时，通常需要显式评估如下代价不等式：

$$T_{\text{gpu}} = T_{\text{H2D}} + T_{\text{launch}} + T_{\text{kernel}} + T_{\text{D2H}} < T_{\text{cpu}}$$

在实际调度器设计中，可以收敛为四条硬约束：

1. **规模与驻留阈值（Amortization Guard）**：单次调用数据量过小（例如低于数百万行或几十 MB）且无法在后续多个算子中继续复用显存驻留状态时，直接留给 CPU，避免 PCIe 和 Kernel 启动开销反噬。
2. **管线级切分优于算子级微调度（Pipeline-Level Offloading）**：尽量以连续的 Pipeline（甚至整个 Stage）为单位做 CPU/GPU 划分，绝不为了一个孤立的算子频繁跨越 `Host <-> Device` 边界。
3. **算子与数据类型亲和性（Affinity Routing）**：定长数值型的大表 Scan、等值 Hash Join、高基数 GroupBy、全局 Radix Sort 优先分配给 GPU；重度依赖复杂正则表达式、非结构化 JSON 解析、高分支 UDF 或极小批量的运算直接路由给 CPU。
4. **显存水位与倾斜熔断（Headroom & Skew Guard）**：调度前结合统计信息估算 Hash Table 膨胀系数与输出基数；当预估显存接近阈值或检测到极端低基数/高倾斜键时，主动切换至 Sort-Based 路径、触发分级 Spill，或直接回退至 CPU 执行，保证系统稳定性不被单条慢查询拖垮。

---

## 五、商业云数仓的真实账本：Snowflake、Databricks、BigQuery 为什么不拿 GPU 跑主体 SQL？

既然 GPU 在特定算子上有数倍乃至十倍的吞吐优势，为什么当我们审视 **Snowflake、Databricks、Google BigQuery** 这三大现代商业云数仓（Cloud Data Warehouse / Lakehouse）时，它们的核心 SQL 引擎却几乎清一色地运行在通用 CPU 集群上？

答案并不在于云厂商写不出 CUDA 算子，而在于一旦把视角从“单机算子微基准测试（Micro-benchmark）”拉升到“多租户云数仓的总拥有成本（TCO）”，GPU 会撞上三道残酷的经济墙。

### 5.1 云原生数仓的三道经济墙：对象存储 I/O、内存单价与潮汐空转

1. **存算分离架构下，真正的瓶颈在对象存储与网络，不在本地算力**：
   现代云数仓全部建立在存算分离（Disaggregated Storage and Compute）之上，冷热数据存放在 S3、GCS 或 ADLS 对象存储中。一条典型商业 BI 查询有 60%–80% 的时间花在通过 25–100 Gbps 网卡拉取 Parquet 分片、解码字符串以及跨节点网络 Shuffle 上。如果在计算节点插上每小时租金高昂的 A100/H100，GPU 绝大部分时间都在空等网卡和磁盘喂数据——**花最贵的算力租金，去等最慢的远程 I/O**。
2. **每美元内存容量（`GB/$`）相差悬殊**：
   分析型查询中包含大量宽表 Join、大窗口函数与高基数聚合，极度消耗内存容量。在公有云上，同等每小时租金的通用内存型 CPU 实例（尤其是 AWS Graviton、Google Axion 等自研 ARM 实例），所能提供的系统内存容量通常是 GPU 实例显存（VRAM）的 **8 到 16 倍**。为了凑齐 1 TB 显存去装一张大表的中间状态，租用多卡 GPU 节点的账单会直接击穿客户的预算红线。
3. **潮汐负载与 Serverless 弹性（Scale-to-Zero）的矛盾**：
   企业 SQL 报表与交互式查询呈现剧烈的脉冲特征（早高峰密集刷新、夜间闲置）。CPU 虚拟仓库（如 Snowflake Virtual Warehouse）可以在秒级完成拉起、扩容与自动休眠（Auto-suspend），闲置成本极低；而高端 GPU 实例不仅基线租金昂贵、云端库存紧缺，且待机功耗与容器冷启动/上下文初始化开销大，很难在按量付费（Pay-as-you-go）的零碎 SQL 负载中摊平成本。

因此，云厂商在核心 SQL 引擎上的默契选择是：**用软件工程压榨廉价 CPU 的硬件极限，而不是让客户为昂贵的 GPU 买单**。例如 Databricks 重金打造了基于 C++ 与 AVX-512 SIMD 的向量化引擎 **Photon**；Snowflake 持续迭代自研 C++ 列式向量化引擎并大规模迁移至 Graviton ARM 处理器；BigQuery 则依靠 **Dremel** 引擎配合 Borg 大规模多租户 CPU 池、Colossus 文件系统与 Jupiter 高速网络，用成千上万个廉价 CPU 核的瞬时并发吃掉 PB 级扫描。

### 5.2 三大云厂商与独立 GPU 数据库的真实分工版图

但这并不意味着云数仓完全不用 GPU，而是它们对 GPU 的使用边界划得极其克制——**关系代数归 CPU，张量与非结构化重算归 GPU**：

| 厂商 / 体系 | 主体 SQL / BI 引擎策略 | GPU 在平台内的真实定位与切入场景 |
| :--- | :--- | :--- |
| **Snowflake** | Virtual Warehouse 100% 运行在 CPU（Graviton / x86）向量化引擎上，不提供 GPU 加速标准 SQL 选项 | 仅在 **Snowpark Container Services (SPCS)** 与 **Cortex AI** 中挂载 GPU，专门用于 LLM 推理/微调、非结构化文档（PDF/图片/音频）解析与向量 Embedding 生成 |
| **Databricks** | 交互式数仓（Databricks SQL）默认依靠 **Photon（纯 CPU C++ 向量化引擎）** 提速降本 | 一方面在 **Mosaic AI** 中将 GPU 用于模型训练与推理；另一方面通过 **Spark RAPIDS** 插件，允许用户在超大规模批处理 ETL 与特征工程管线中按需挂载 T4/L4/A100 集群 |
| **Google BigQuery** | Dremel 引擎全程在多租户 CPU 池与 Jupiter 网络上完成扫描、Join 与 Shuffle | 当 SQL 中调用 `BQML`、`AI.GENERATE` 或多模态向量检索时，通过远程联邦调用将重算部分卸载至 **Vertex AI（GPU/TPU）**，算完再把结果列拼回 CPU 关系计划 |
| **独立 GPU 数据库**（HeavyDB / Kinetica / Theseus） | 早期试图正面取代通用 CPU 数仓的方案（如 BlazingSQL）多已退场或转型 | 存活者分化为两条窄路：一是 **HeavyDB / Kinetica** 死磕电信/军工/地理空间（Geospatial）毫秒级原位渲染与实时流分析；二是 **Voltron Data（Theseus）** 聚焦多机 NVLink + RDMA 上的百 TB 级超大规模 ETL Shuffle |

---

## 六、学术界十五年演进：从“片上算子炫技”到“TCO 归一化与软硬协同”

与工业界的克制形成呼应，数据库学术界（SIGMOD / VLDB / CIDR）对 GPU 加速的研究在过去十五年里也经历了一场深刻的祛魅与转向，大致可以分为三代演进脉络：

### 6.1 第一代（算子原语与片上编译融合，~2020）：把显存带宽压榨到极致

早期研究往往假设“整个数据集已经装在 GPU 显存里”，重点解决如何消除算子间物化与 SIMT 分支分化：

- **Crystal（Shanbhag et al., SIGMOD 2020）**：从第一性原理建模对比了 CPU 与 GPU 的微架构，明确指出 GPU 跑 OLAP 的瓶颈几乎完全在于**显存带宽而非算力**。Crystal 提出了 **Tile-based（分块）执行模型**，不再按算子全列读写 HBM，而是以 Thread Block 为单位将数据切片加载进片上 Shared Memory 和寄存器，在片上一次性跑完整个查询管线（Filter -> Probe -> Aggregate）。
- **DogQC（Funke & Teubner, VLDB 2020）与 HetExchange（Chrysogelos et al., VLDB 2019）**：DogQC 针对非均匀数据过滤与 Join 展开后引发的严重 Warp 分化，在 JIT 编译生成的 CUDA 管线中插入基于共享内存的动态负载均衡器（Divergence Buffer）；HetExchange 则在 EPFL 的 JIT 引擎中设计了统一的异构交换算子，让同一个流水线能够同时调度多核 CPU 与多块 GPU 并行跑批。

### 6.2 第二代（打破 PCIe 与显存容量墙，2020–2024）：快互联、SSD 直通与借用张量核

当审视点回到“真实数据库远大于显存容量”时，研究重心转向了硬件拓扑突破与新型计算单元借用：

- **高速互联下的核外执行（Lutz et al., *"Pump Up the Volume"*, SIGMOD 2020）**：针对“数据溢出显存后 GPU 必输 CPU”的传统定论，该工作在具备 **NVLink 2.0**（双向高带宽一致性互联）的平台上重新设计了核外（Out-of-Core）无分区 Hash Join，证明当主机内存到显存的通道足够宽时，哪怕大表放在主机内存流式喂给 GPU，依然能比 PCIe 3.0 快 18 倍、比多核 CPU 快 7.3 倍。
- **SSD 直通与语义缓存（GOLAP, SIGMOD 2023；Mordred, PVLDB）**：**GOLAP** 提出 *GPU-in-data-path* 架构，通过 GPUDirect 让压缩列块从 NVMe SSD 阵列直接流式打入显存，在扫描同时由 GPU 内核实时解压并做谓词裁剪；**Mordred** 则在 CPU-GPU 混合引擎中引入语义感知缓存（Semantic-aware Caching）与分片级协同执行，用细粒度流水掩盖 PCIe 传输。
- **向 AI 硬件借算力（TCUDB, SIGMOD 2022）**：现代 GPU 芯片面积与算力的大头都给了服务于深度学习的 **Tensor Core Units（TCU）**，而传统 SQL 只能用旁边的普通 CUDA 核心。TCUDB 将关系型自然连接（Natural Join）与分组聚合重写为稀疏/稠密矩阵乘法（GEMM），强行唤醒闲置的 Tensor Core 来跑关系代数。

### 6.3 第三代（可组合引擎与单位成本对决，2025–2026）：Sirius 与 Maximus 的反思

到了最近两年，学术界终于直面工业界最关心的问题：**“不改现有数据库代码怎么接 GPU？”** 以及 **“算上云主机差价，到底省不省钱？”**

- **Maximus（PVLDB）与 Sirius（CIDR 2026）**：以 **Sirius** 为代表的新一代研究放弃了从头造一个封闭 GPU 数据库的做法，通过 **Substrait** 挂载到 DuckDB 等成熟系统之后，在评测方法论上做了一个关键纠偏——不再只比绝对延时，而是按**公有云相同每小时租金（Normalized Cost / `$/Query`）**对比。研究发现，只要规避掉频繁回退并利用三级内存管理跑通中大型分析查询（如 TPC-H SF100），1 块中端数据中心 GPU（如 L4 / A10G / A100）在单查询耗时上的缩减幅度，足以抵消其相比同等租金 CPU 节点的单价劣势。

---

## 七、架构师判断：在 GPU 如此昂贵的今天，如何决策“GPU 与数据库的融合”？

回到最核心的工程决策问题：**今天硬件（尤其是高端 GPU 和 HBM）这么贵，我们在设计或演进数据系统时，到底该如何看待 GPU 与数据库的融合？**

这里给出一个明确的判断框架：**在通用数仓里“用 GPU 全面替代 CPU”是一个伪命题；但在特定数据链路与新一代硬件拓扑下，“按需局部融合”正在从赔钱货变成高 ROI 利器。**

### 7.1 为什么说“全面替代”是伪命题，而“三个交汇点”是真需求？

如果你的目标是加速日常企业 BI 报表、高并发点查、或者满是脏字符串清洗与小表 Join 的常规数仓负载，**请把预算投在 CPU 向量化（如引入 Velox / DuckDB / Photon 思路）、更好的列存编码（字典下推、延迟物化）和本地 SSD 缓存上**。在这些场景下买 GPU，90% 的经费都会浪费在等待远程存储 I/O、PCIe 搬运和长尾函数回退上。

真正值得将 GPU 与数据库算子深度融合的，只有以下三个算得过来经济账的场景：

1. **AI-Data 同域流水线（AI-Adjacent Data Processing，当前最高确定性场景）**：
   当 SQL/ETL 的下游本来就是运行在 GPU 上的大模型微调、批量推理（如 `SELECT AI_EXTRACT(...) JOIN ...`）、多模态数据预处理或向量索引（IVF-PQ / CAGRA）构建时，数据终究是要上卡的。此时用 `cuDF` / `cudf-polars` / `Sirius` 在显存内就地把前置的特征过滤、宽表 Join 和窗口聚合一起做掉，省下的不仅是计算时间，更是避免了 `CPU 内存 -> 序列化 -> PCIe -> GPU 显存` 的双份机器开销。
2. **重算子弹性卸载池（Disaggregated Heavy-Stage Offloading）**：
   不要给整个数仓集群的每个节点都绑上昂贵的 GPU，更不要拿 H100 去跑普通 SQL。正确的架构是借用 **Substrait + 可组合 GPU Worker（如 Sirius 或 Velox-cuDF）**，在纯 CPU 数仓旁挂载一个极小规模的**高性价比推理卡资源池（如 NVIDIA L4 / L40S，而非昂贵的训练卡 H100）**。优化器仅在检测到“百 GB 级纯数值大表多路 Hash Join / 全局重排序 / 超高基数聚合”且算术/访存密集度达标时，将该 Stage 弹性切给 GPU 池，算完毫秒级释放，把 GPU 利用率打满。
3. **统一内存架构（GH200 / CXL / APU）下的“零拷贝借带宽”**：
   过去十年的异构调度之所以痛苦，根源在于 PCIe 这道窄门。而在 NVIDIA Grace Hopper（GH200，900 GB/s NVLink-C2C）或 AMD MI300A 等 CPU+GPU 紧耦合架构上，GPU 可以直接以远超 PCIe 的带宽访问主机大内存。一旦省去了显式 H2D/D2H 拷贝税，数据库完全可以把主存当作容量层、把 GPU 当作片上高带宽吞吐加速器，按算子亲和性在微秒级无感切换。

### 7.2 动手前的四步量化验算（Go / No-Go 清单）

在决定是否为某个数据库模块或业务流水线引入 GPU 之前，不妨先按顺序算清这四道题，只要有一道题答“否”，就不应轻易上马：

1. **看端到端 I/O 占比（Amdahl 过滤）**：当前作业剖析（Profile）中，等待对象存储网络读取、CPU 解压和反序列化的时间是否低于总耗时的 40%？（如果 70% 时间都在等 S3 网卡，GPU 把计算加速到 0 秒也只能提速 30%，除非能同步上马 GPUDirect Storage + nvCOMP）。
2. **看显存驻留深度（Residency 过滤）**：数据一旦跨过 PCIe 进入显存，能否在**不回退 CPU** 的前提下，连续完成至少 3 个以上重度算子（例如 `Filter -> Hash Join -> GroupBy -> OrderBy`），并在显存内将结果体积收敛一个数量级以上？
3. **算云租用盈亏平衡点（`$/Query` 过滤）**：设目标 GPU 实例（建议优先评估 L4/A10G 等推理卡）的每小时租金是当前 CPU 节点的 `K` 倍（通常 `K` 在 2.5 到 6 之间）。包含 PCIe 传输与回退开销在内的**端到端加速比**能否稳定超过 `K` 倍？只有当 `Speedup > K` 时，单次查询的云账单（`$/Query`）才是真正下降的。
4. **看长尾维护税（Engineering Tax 过滤）**：能否直接通过 **Substrait + `libcudf`** 这种标准中间层复用社区算子，并让宿主 CPU 引擎（如 DuckDB / Spark / Velox）兜底长尾标量函数？**切忌在业务团队内部从零手写和维护庞大的 SQL 标量函数 CUDA 库**，那会变成永远填不完的兼容性黑洞。

### 7.3 全景选型对照表

| 评估维度 | 纯 CPU 向量化引擎（DuckDB / Polars / Photon） | 透明代理 / 混合插件（`cudf.pandas` / `spark-rapids`） | GPU-Native 可组合引擎（`Sirius` / `libcudf` 弹性池） |
| :--- | :--- | :--- | :--- |
| **核心优势** | 零传输开销、大内存单价极低、全 SQL 覆盖、弹性休眠友好 | 零代码或低侵入接入现有生态，兼容性兜底完善 | 全管线显存驻留、极致 HBM 吞吐、三级内存抗 OOM |
| **最容易踩的坑** | 超大规模数值 Join/Sort 受限于 DDR5 带宽上限 | 热路径回退引发 PCIe 乒乓与试错税；常驻挂载 GPU 账单贵 | 继承 CPU 代价模型导致计划次优；长尾函数仍需回退 CPU |
| **最佳甜蜜点** | 通用云数仓、日常 BI 报表、重字符串/JSON 清洗、脉冲负载 | 已有重度 Pandas/Spark 特征工程管线，与下游 GPU 训练同域 | 大规模数值型重算 Stage 卸载、AI-Data 同域预处理、紧耦合硬件 |

归根结底，GPU 不是一块“插上就能降本增效”的通用数据库加速卡，而是一台**高吞吐、高租金、高启动门槛、且极度依赖显存局部性的流式协处理器**。认清 PCIe 与云存储 I/O 的物理边界，放弃“全面替代 CPU”的执念，在 **AI-Data 同域管线** 与 **弹性重算子卸载** 上用好 `libcudf` 与 `Sirius` 这类可组合底座，才是硬件昂贵时代最清醒的架构选择。

---

> **参考资料与调研基线**
> - **工业界架构与云数仓实践**：Databricks *Photon: A Fast Query Engine for Lakehouse Systems* (SIGMOD 2022) · Snowflake Virtual Warehouses & Snowpark Container Services (SPCS) 架构文档 · Google BigQuery Dremel & Vertex AI 联邦推理架构 · NVIDIA RAPIDS (`libcudf`, `cuCollections`, `cudf.pandas`, `spark-rapids`, `cudf-polars`) 官方技术文档。
> - **学术界代表性论文**：
>   - **Sirius**: *Rethinking Analytical Processing in the GPU Era* ([CIDR 2026](https://www.cidrdb.org/cidr2026/papers/p12-yogatama.pdf))
>   - **Crystal**: Shanbhag et al., *A Study of the Fundamental Performance Characteristics of GPUs and CPUs for Database Analytics* (SIGMOD 2020)
>   - **NVLink Out-of-Core Join**: Lutz et al., *Pump Up the Volume: Processing Large Data on GPUs with Fast Interconnects* (SIGMOD 2020)
>   - **HetExchange & DogQC**: Chrysogelos et al., *HetExchange: Encapsulating Heterogeneous CPU-GPU Parallelism in JIT Compiled Engines* (VLDB 2019) · Funke & Teubner, *Data-Parallel Query Processing on Non-Uniform Data* (VLDB 2020)
>   - **GOLAP / TCUDB / Mordred / Maximus**: *GOLAP: A GPU-in-Data-Path Architecture for High-Speed OLAP* (SIGMOD) · *TCUDB: Accelerating Database with Tensor Processors* (SIGMOD 2022) · *Mordred* & *Maximus* (PVLDB).
